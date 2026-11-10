"""Deploys replace instances through the normal path (apps/rollout.py): add new ones, wait until promote put them
live, then remove the old ones. A fake node + promote stands in for Docker and the node network here."""
import asyncio

import pytest
from fastapi import HTTPException


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def env(client, monkeypatch):
    """A remote node, the primary, an app on the base domain; build, source and nginx are stubbed."""
    from sqlalchemy import delete
    from apps import nginx_sync, rollout
    from core.database import AsyncSessionLocal
    from core.models import Application, ApplicationReplica, Node
    from nodes.registry import ensure_local_node

    monkeypatch.setattr(rollout, "POLL_SECONDS", 0.02)
    monkeypatch.setattr(nginx_sync._syscfg, "get_base_domain_cached", lambda: "example.test")
    built = []

    async def fake_build(app, push):
        built.append(app.id)
    monkeypatch.setattr(rollout, "_build_here", fake_build)
    monkeypatch.setattr(rollout, "_refresh_app_source_revision", lambda app: "rev-2")
    refreshed = []

    async def fake_refresh(app_id):
        refreshed.append(app_id)
    monkeypatch.setattr(rollout.nginx_sync, "refresh_app_nginx", fake_refresh)
    made = {"apps": [], "nodes": []}

    class Env:
        built_here = built
        nginx = refreshed

        def node(self, name, status="online"):
            async def go():
                async with AsyncSessionLocal() as db:
                    row = Node(name=name, status=status, auth_token=f"token-{name}", is_local=False, enabled=True,
                               wg_ip="10.77.0.9", agent_version="2.0.0")
                    db.add(row)
                    await db.commit()
                    return row.id
            node_id = _run(go())
            made["nodes"].append(node_id)
            return node_id

        def local(self):
            async def go():
                async with AsyncSessionLocal() as db:
                    return (await ensure_local_node(db)).id
            return _run(go())

        def app(self, name, instances, **fields):
            """instances: list of (node_id, status, own cpu limit)."""
            async def go():
                async with AsyncSessionLocal() as db:
                    app = Application(name=name, repo_url="https://example.test/x", start_command="x", port=3000,
                                      working_dir=".", **fields)
                    db.add(app)
                    await db.flush()
                    ids = []
                    for i, (node_id, status, cpu) in enumerate(instances):
                        row = ApplicationReplica(app_id=app.id, node_id=node_id, status=status, desired="running",
                                                 external_port=12000 + app.id * 10 + i, docker_cpu_limit=cpu)
                        db.add(row)
                        await db.flush()
                        ids.append(row.id)
                    await db.commit()
                    return app.id, ids
            app_id, ids = _run(go())
            made["apps"].append(app_id)
            return app_id, ids

        def rows(self, app_id):
            async def go():
                from sqlalchemy import select
                async with AsyncSessionLocal() as db:
                    return (await db.execute(select(ApplicationReplica).where(ApplicationReplica.app_id == app_id)
                                             .order_by(ApplicationReplica.id))).scalars().all()
            return _run(go())

    yield Env()

    async def cleanup():
        async with AsyncSessionLocal() as db:
            await db.execute(delete(ApplicationReplica).where(ApplicationReplica.app_id.in_(made["apps"])))
            await db.execute(delete(Application).where(Application.id.in_(made["apps"])))
            await db.execute(delete(Node).where(Node.id.in_(made["nodes"])))
            await db.commit()
    _run(cleanup())


async def _fake_promote(app_id, known: set, outcome, seen: list, stop: asyncio.Event):
    """Plays node + promote: every new 'starting' instance goes live (or fails, as `outcome(replica)` says).
    Records which instances existed at the moment each new one went live."""
    from sqlalchemy import select
    from core.database import AsyncSessionLocal
    from core.models import ApplicationReplica
    while not stop.is_set():
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(ApplicationReplica).where(ApplicationReplica.app_id == app_id))).scalars().all()
            for row in rows:
                if row.id not in known and row.status == "starting":
                    known.add(row.id)
                    status = outcome(row)
                    row.status = status
                    row.last_error = "health check failed: 500" if status == "error" else None
                    seen.append((row.id, status, sorted(r.id for r in rows)))
            await db.commit()
        await asyncio.sleep(0.02)


def _deploy(app_id, batch, outcome=lambda row: "running"):
    from apps import rollout
    from core.database import AsyncSessionLocal
    seen: list = []

    async def go():
        known: set = set()
        async with AsyncSessionLocal() as db:
            from sqlalchemy import select
            from core.models import ApplicationReplica
            known |= {r.id for r in (await db.execute(select(ApplicationReplica).where(ApplicationReplica.app_id == app_id))).scalars()}
        stop = asyncio.Event()
        fake = asyncio.create_task(_fake_promote(app_id, known, outcome, seen, stop))
        try:
            async with AsyncSessionLocal() as db:
                return await rollout.replace_instances(app_id, db, "test", batch=batch)
        finally:
            stop.set()
            await fake
    return _run(go()), seen


def test_blue_green_starts_all_new_instances_before_removing_any_old_one(env):
    remote, local = env.node("ro-bg"), env.local()
    app_id, old = env.app("ro-bg-app", [(remote, "running", 0.5), (local, "running", None)])
    result, seen = _deploy(app_id, batch=None)
    rows = env.rows(app_id)
    assert result["replaced"] == 2 and {r.id for r in rows}.isdisjoint(old)
    assert sorted(r.node_id for r in rows) == sorted([remote, local])          # same nodes
    assert all(r.status == "running" and r.desired == "running" and r.spec_hash for r in rows)
    assert {r.node_id: r.docker_cpu_limit for r in rows}[remote] == 0.5         # an instance's own limits are kept
    assert all(set(old) <= set(existing) for _, _, existing in seen)           # every new one went live next to all old ones
    assert env.built_here == [app_id]                                           # the primary runs one: built here first


def test_rolling_replaces_one_at_a_time_and_never_leaves_the_app_without_its_old_instances_too_early(env):
    remote = env.node("ro-roll")
    app_id, old = env.app("ro-roll-app", [(remote, "running", None), (remote, "running", None), (remote, "running", None)])
    result, seen = _deploy(app_id, batch=1)
    assert result["replaced"] == 3 and len(seen) == 3
    for k, (_, status, existing) in enumerate(seen):
        assert status == "running"
        assert set(old[k:]) <= set(existing)          # when new #k went live, old #k and the ones after it were still there
        assert not set(old[:k]) & set(existing)       # and the ones before it were already gone
    assert {r.id for r in env.rows(app_id)}.isdisjoint(old)
    assert env.built_here == []                                                 # only a remote node: it builds itself


def test_a_new_instance_that_fails_is_removed_and_the_old_ones_keep_serving(env):
    remote = env.node("ro-fail")
    app_id, old = env.app("ro-fail-app", [(remote, "running", None), (remote, "running", None)])
    with pytest.raises(HTTPException) as err:
        _deploy(app_id, batch=1, outcome=lambda row: "error")
    assert err.value.status_code == 502 and "rolled back" in err.value.detail and "health check failed" in err.value.detail
    rows = env.rows(app_id)
    assert [r.id for r in rows] == old and all(r.status == "running" for r in rows)
    assert app_id in env.nginx                                                  # nginx followed the removal


def test_a_failure_in_the_second_round_keeps_what_the_first_round_did(env):
    remote = env.node("ro-half")
    app_id, old = env.app("ro-half-app", [(remote, "running", None), (remote, "running", None)])
    first = []

    def outcome(row):
        first.append(row.id)
        return "running" if len(first) == 1 else "error"
    with pytest.raises(HTTPException):
        _deploy(app_id, batch=1, outcome=outcome)
    ids = [r.id for r in env.rows(app_id)]
    assert ids == sorted([old[1], first[0]])                  # old #1 replaced by the first new one; old #2 still serves


def test_an_app_with_nothing_running_gets_one_new_instance_on_its_node(env):
    remote = env.node("ro-none")
    app_id, old = env.app("ro-none-app", [(remote, "stopped", None)])
    result, _ = _deploy(app_id, batch=None)
    rows = env.rows(app_id)
    assert result["replaced"] == 1 and len(rows) == 2
    assert [r.status for r in rows] == ["stopped", "running"] and rows[1].node_id == remote


def test_an_offline_node_stops_the_deploy_before_anything_changes(env):
    remote = env.node("ro-offline", status="offline")
    app_id, old = env.app("ro-offline-app", [(remote, "running", None)])
    with pytest.raises(HTTPException) as err:
        _deploy(app_id, batch=None)
    assert err.value.status_code == 400 and "offline" in err.value.detail
    assert [r.id for r in env.rows(app_id)] == old


def test_without_a_route_there_is_nothing_to_switch(env, monkeypatch):
    from apps import nginx_sync
    monkeypatch.setattr(nginx_sync._syscfg, "get_base_domain_cached", lambda: "")
    remote = env.node("ro-noroute")
    app_id, old = env.app("ro-noroute-app", [(remote, "running", None)])
    with pytest.raises(HTTPException) as err:
        _deploy(app_id, batch=None)
    assert err.value.status_code == 400


@pytest.mark.parametrize("status,needle", [("node_offline", "went offline"), ("error", "failed")])
def test_waiting_stops_when_the_instance_cannot_get_live(env, status, needle):
    from apps import rollout
    remote = env.node(f"ro-wait-{status}")
    app_id, (rid,) = env.app(f"ro-wait-{status}-app", [(remote, status, None)])
    with pytest.raises(rollout.RolloutFailed, match=needle):
        _run(rollout.wait_until_live(app_id, rid, lambda line: None))


def test_waiting_has_a_limit(env, monkeypatch):
    from apps import rollout
    monkeypatch.setattr(rollout, "START_TIMEOUT_SECONDS", 0)
    remote = env.node("ro-wait-slow")
    app_id, (rid,) = env.app("ro-wait-slow-app", [(remote, "starting", None)], health_timeout_seconds=1)
    with pytest.raises(rollout.RolloutFailed, match="not live"):
        _run(rollout.wait_until_live(app_id, rid, lambda line: None))


def test_waiting_for_a_removed_instance_stops(env):
    from apps import rollout
    with pytest.raises(rollout.RolloutFailed, match="removed"):
        _run(rollout.wait_until_live(env.app("ro-wait-gone-app", [])[0], 999999, lambda line: None))


def test_a_worker_goes_live_once_its_node_runs_it(env):
    """No port to connect to: promote must not wait for a status only it can set (it used to give up after 90s)."""
    from nodes import promote
    remote = env.node("ro-worker")
    app_id, (rid,) = env.app("ro-worker-app", [(remote, "starting", None)], no_web=True)
    _run(promote.promote_when_reachable(rid))
    assert env.rows(app_id)[0].status == "running"
