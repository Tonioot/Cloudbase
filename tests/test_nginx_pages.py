"""What nginx shows follows from the app and its instances (apps/nginx_sync.py _nginx_mode): no timers, nothing to undo.

update / maintenance mode first; the app as soon as one instance is live; the restart page while instances that ran
before come back up; the start page while new ones come up; otherwise the downtime page ('normal' with no backends).
"""
import asyncio

import pytest


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def nginx(monkeypatch):
    """Every config the panel writes, as the mode it was written with; nothing touches the real nginx."""
    from apps import nginx_sync
    written = []
    real_generate = nginx_sync.nginx_config.generate_config

    def generate(*args, **kwargs):
        written.append(kwargs["mode"])
        return real_generate(*args, **kwargs)
    monkeypatch.setattr(nginx_sync.nginx_config, "generate_config", generate)
    monkeypatch.setattr(nginx_sync.nginx_files, "write_nginx_config", lambda key, config: (True, "ok"))
    monkeypatch.setattr(nginx_sync, "_ensure_maintenance_files", lambda app, app_id: (True, "ok"))
    monkeypatch.setattr(nginx_sync._syscfg, "get_base_domain_cached", lambda: "example.test")
    monkeypatch.setattr(nginx_sync._syscfg, "get_base_ssl_cert_cached", lambda: "/etc/ssl/wildcard.pem")   # no certbot
    return written


@pytest.fixture
def make(client):
    from sqlalchemy import delete
    from core.database import AsyncSessionLocal
    from core.models import Application, ApplicationReplica
    from nodes.registry import ensure_local_node
    made = []

    def app_with(name, instances):
        """instances: list of (status, desired, container_id)."""
        async def go():
            async with AsyncSessionLocal() as db:
                node = await ensure_local_node(db)
                app = Application(name=name, repo_url="https://example.test/x", start_command="x", port=3000)
                db.add(app)
                await db.flush()
                ids = []
                for i, (status, desired, container) in enumerate(instances):
                    row = ApplicationReplica(app_id=app.id, node_id=node.id, status=status, desired=desired,
                                             container_id=container, external_port=11000 + app.id * 10 + i)
                    db.add(row)
                    await db.flush()
                    ids.append(row.id)
                await db.commit()
                return app.id, ids
        app_id, ids = _run(go())
        made.append(app_id)
        return app_id, ids

    yield app_with

    async def cleanup():
        async with AsyncSessionLocal() as db:
            await db.execute(delete(ApplicationReplica).where(ApplicationReplica.app_id.in_(made)))
            await db.execute(delete(Application).where(Application.id.in_(made)))
            await db.commit()
    _run(cleanup())


def _mode(app_id, **app_fields):
    from apps import nginx_sync
    from core.database import AsyncSessionLocal
    from core.models import Application

    async def go():
        async with AsyncSessionLocal() as db:
            app = await db.get(Application, app_id)
            for k, v in app_fields.items():
                setattr(app, k, v)
            backends = await nginx_sync._get_nginx_backends(app, db)
            return await nginx_sync._nginx_mode(app, db, backends)
    return _run(go())


def _set(replica_id, **fields):
    from core.database import AsyncSessionLocal
    from core.models import ApplicationReplica

    async def go():
        async with AsyncSessionLocal() as db:
            row = await db.get(ApplicationReplica, replica_id)
            for k, v in fields.items():
                setattr(row, k, v)
            await db.commit()
    _run(go())


# ── the rule ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("instances,expected", [
    ([("running", "running", "c1")], "normal"),                                          # live: the app itself
    ([("running", "running", "c1"), ("starting", "running", None)], "normal"),            # one live is enough
    ([("starting", "running", None)], "starting"),                                       # new: start page
    ([("pending", "running", None)], "starting"),
    ([("starting", "running", "c-old")], "restart"),                                     # ran before: restart page
    ([("starting", "running", None), ("starting", "running", "c-old")], "restart"),
    ([("error", "running", None)], "normal"),                                            # failed: downtime page
    ([("node_offline", "running", "c1")], "normal"),                                     # unreachable: downtime page
    ([("stopping", "stopped", "c1")], "normal"),                                         # stopping: downtime page
    ([("starting", "stopped", None)], "normal"),                                         # not wanted any more
    ([], "normal"),
])
def test_the_page_follows_the_instances(make, instances, expected):
    app_id, _ = make(f"page-{abs(hash(str(instances))) % 10**6}", instances)
    assert _mode(app_id) == expected


def test_update_and_maintenance_mode_win_over_everything(make):
    app_id, _ = make("page-modes", [("running", "running", "c1")])
    assert _mode(app_id, maintenance_mode=True) == "maintenance"
    assert _mode(app_id, update_mode=True) == "update"
    starting, _ = make("page-modes-2", [("starting", "running", None)])
    assert _mode(starting, maintenance_mode=True) == "maintenance"


# ── through the real actions ──────────────────────────────────────────────────
def test_start_shows_the_start_page_until_an_instance_is_live(admin, make, nginx):
    from apps import nginx_sync
    app_id, (rid,) = make("page-start", [("stopped", "stopped", None)])
    assert admin.post(f"/api/apps/{app_id}/start").status_code == 200
    assert nginx[-1] == "starting"
    _set(rid, status="running", container_id="c-new")                 # what promote does once it answers
    _run(nginx_sync.refresh_app_nginx(app_id))
    assert nginx[-1] == "normal"


def test_restart_shows_the_restart_page_and_two_restarts_in_a_row_do_not_undo_it(admin, make, nginx):
    app_id, (rid,) = make("page-restart", [("running", "running", "c-old")])
    assert admin.post(f"/api/apps/{app_id}/restart").status_code == 200
    assert nginx[-1] == "restart"
    assert admin.post(f"/api/apps/{app_id}/restart").status_code == 200
    assert nginx[-1] == "restart"                                      # no timer of the first one to clear it


def test_a_start_that_is_given_up_leaves_the_start_page(make, nginx):
    from nodes import promote
    app_id, (rid,) = make("page-give-up", [("starting", "running", None)])
    _run(promote._give_up(rid, "health check failed: 500"))
    assert nginx[-1] == "normal"                                       # downtime page, not 'starting' forever


def test_a_node_report_that_changes_a_status_rewrites_nginx(make, nginx, monkeypatch):
    from nodes import observed
    app_id, (rid,) = make("page-report", [("starting", "running", None)])     # was not live: only the status change says so
    _set(rid, spec_hash="abc")
    monkeypatch.setattr(observed.promote, "start_promotion", lambda replica_id: None)

    from core.database import AsyncSessionLocal
    from core.models import ApplicationReplica

    async def node_of():
        async with AsyncSessionLocal() as db:
            return (await db.get(ApplicationReplica, rid)).node_id
    node_id = _run(node_of())
    _run(observed.apply(node_id, {str(rid): {"state": "error", "error": "crashed", "spec_hash": "abc"}}))
    assert nginx[-1] == "normal"
    before = len(nginx)
    _run(observed.apply(node_id, {str(rid): {"state": "error", "error": "crashed", "spec_hash": "abc"}}))
    assert len(nginx) == before                                        # nothing changed: nothing written
