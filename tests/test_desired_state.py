"""Nodes run what the primary wants (desired state) instead of obeying commands: the primary's side.

What the primary writes down (nodes/desired.py), the list a node asks for (nodes/snapshot.py), how a
node's report becomes a status (nodes/observed.py), and the places that used to send commands.
"""
import asyncio

import pytest


def _run(coro):
    return asyncio.run(coro)


PAYLOAD = {"app_id": 1, "app_name": "shop", "internal_port": 3000, "external_port": 9100,
           "env_vars": {"SECRET_KEY": "s3cret"}, "source_revision": "abc123", "docker_options": {"restart_policy": "no"}}


# ── helpers: rows straight in the database ─────────────────────────────────────
def _node(name, token=None, status="online"):
    from core.database import AsyncSessionLocal
    from core.models import Node

    async def go():
        async with AsyncSessionLocal() as db:
            node = Node(name=name, role="node", status=status, auth_token=token or f"token-{name}", is_local=False, enabled=True,
                        wg_ip="127.0.0.1", wg_public_key="K" * 44, wg_state="up", agent_version="2.0.0")
            db.add(node)
            await db.commit()
            return node.id, node.auth_token
    return _run(go())


def _app(name):
    from core.database import AsyncSessionLocal
    from core.env_crypto import encrypt_env
    from core.models import Application

    async def go():
        async with AsyncSessionLocal() as db:
            app = Application(name=name, repo_url="https://example.test/x", start_command="npm start", port=3000,
                              status="running", env_vars=encrypt_env({"A": "1"}))
            db.add(app)
            await db.commit()
            return app.id
    return _run(go())


def _replica(app_id, node_id, port, status="starting", desired=None):
    from core.database import AsyncSessionLocal
    from core.models import ApplicationReplica

    async def go():
        async with AsyncSessionLocal() as db:
            rep = ApplicationReplica(app_id=app_id, node_id=node_id, external_port=port, status=status, desired=desired)
            db.add(rep)
            await db.commit()
            return rep.id
    return _run(go())


def _row(replica_id):
    from core.database import AsyncSessionLocal
    from core.models import ApplicationReplica

    async def go():
        async with AsyncSessionLocal() as db:
            return await db.get(ApplicationReplica, replica_id)
    return _run(go())


def _start(node_id, replica_id, payload=None):
    from core.database import AsyncSessionLocal
    from nodes import desired

    async def go():
        async with AsyncSessionLocal() as db:
            digest = await desired.start(db, node_id=node_id, replica_id=replica_id, payload=payload or PAYLOAD)
            await db.commit()
            return digest
    return _run(go())


def _stop(node_id, replica_id):
    from core.database import AsyncSessionLocal
    from nodes import desired

    async def go():
        async with AsyncSessionLocal() as db:
            await desired.stop(db, node_id=node_id, replica_id=replica_id)
            await db.commit()
    _run(go())


def _snapshot(node_id):
    from core.database import AsyncSessionLocal
    from nodes import snapshot

    async def go():
        async with AsyncSessionLocal() as db:
            return await snapshot.snapshot(db, node_id)
    return _run(go())


@pytest.fixture
def setup(client):
    """A node, an app and a clean-up afterwards."""
    from sqlalchemy import delete
    from core.database import AsyncSessionLocal
    from core.models import Application, ApplicationReplica, Node, NodeCommand
    made = {"nodes": [], "apps": []}

    def node(name):
        node_id, token = _node(name)
        made["nodes"].append(node_id)
        return node_id, token

    def app(name):
        app_id = _app(name)
        made["apps"].append(app_id)
        return app_id

    yield node, app

    async def cleanup():
        async with AsyncSessionLocal() as db:
            await db.execute(delete(ApplicationReplica).where(ApplicationReplica.app_id.in_(made["apps"])))
            await db.execute(delete(Application).where(Application.id.in_(made["apps"])))
            await db.execute(delete(NodeCommand).where(NodeCommand.node_id.in_(made["nodes"])))
            await db.execute(delete(Node).where(Node.id.in_(made["nodes"])))
            await db.commit()
    _run(cleanup())


# ── what the primary writes down ───────────────────────────────────────────────
def test_starting_an_instance_stores_an_encrypted_spec_and_a_hash(setup):
    node, app = setup
    node_id, _ = node("d-start")
    rid = _replica(app("d-start-app"), node_id, 9101)
    digest = _start(node_id, rid)
    row = _row(rid)
    assert row.desired == "running" and row.spec_hash == digest and len(digest) == 16
    assert "s3cret" not in row.spec_json and "shop" not in row.spec_json          # not readable in the database
    spec = _snapshot(node_id)["replicas"][0]["spec"]
    assert spec["env_vars"] == {"SECRET_KEY": "s3cret"} and spec["replica_id"] == rid and spec["external_port"] == 9100


def test_every_start_gets_a_new_hash_so_a_restart_recreates_the_container(setup):
    node, app = setup
    node_id, _ = node("d-restart")
    rid = _replica(app("d-restart-app"), node_id, 9102)
    assert _start(node_id, rid) != _start(node_id, rid)


def test_stopping_keeps_the_row_and_the_list_says_stopped(setup):
    node, app = setup
    node_id, _ = node("d-stop")
    rid = _replica(app("d-stop-app"), node_id, 9103)
    _start(node_id, rid)
    _stop(node_id, rid)
    entry = _snapshot(node_id)["replicas"][0]
    assert entry["desired"] == "stopped" and entry["spec"] is None and _row(rid) is not None


def test_the_list_only_has_the_instances_of_that_node(setup):
    node, app = setup
    a, _ = node("d-a")
    b, _ = node("d-b")
    app_id = app("d-two-nodes")
    ra, rb = _replica(app_id, a, 9104), _replica(app_id, b, 9105)
    _start(a, ra)
    _start(b, rb)
    assert [e["replica_id"] for e in _snapshot(a)["replicas"]] == [ra]
    assert [e["replica_id"] for e in _snapshot(b)["replicas"]] == [rb]


def test_the_list_hash_changes_with_the_list_and_not_otherwise(setup):
    node, app = setup
    node_id, _ = node("d-hash")
    rid = _replica(app("d-hash-app"), node_id, 9106)
    _start(node_id, rid)
    first = _snapshot(node_id)["hash"]
    assert _snapshot(node_id)["hash"] == first
    _stop(node_id, rid)
    assert _snapshot(node_id)["hash"] != first


def test_instances_from_before_desired_state_get_a_spec_the_first_time_a_node_asks(setup):
    node, app = setup
    node_id, _ = node("d-legacy")
    rid = _replica(app("d-legacy-app"), node_id, 9107, status="running", desired="running")
    assert _row(rid).spec_json is None
    entry = _snapshot(node_id)["replicas"][0]
    assert entry["desired"] == "running" and entry["spec"]["external_port"] == 9107 and entry["spec"]["env_vars"] == {"A": "1"}
    assert _row(rid).spec_hash == entry["spec_hash"]
    assert _snapshot(node_id)["replicas"][0]["spec_hash"] == entry["spec_hash"]       # written once, not every time


# ── the endpoints ────────────────────────────────────────────────────────────────
def test_a_node_asks_for_its_list_and_gets_nothing_back_when_it_has_the_latest(client, setup):
    node, app = setup
    node_id, token = node("d-http")
    rid = _replica(app("d-http-app"), node_id, 9108)
    _start(node_id, rid)
    first = client.get("/api/nodes/agent/desired", headers={"X-Node-Token": token}).json()
    assert [e["replica_id"] for e in first["replicas"]] == [rid]
    again = client.get("/api/nodes/agent/desired", params={"have": first["hash"]}, headers={"X-Node-Token": token}).json()
    assert again == {"hash": first["hash"], "unchanged": True}
    assert client.get("/api/nodes/agent/desired", params={"have": "old"}, headers={"X-Node-Token": token}).json()["replicas"]


def test_the_list_and_the_report_need_a_node_token(client):
    assert client.get("/api/nodes/agent/desired").status_code == 401
    assert client.get("/api/nodes/agent/desired", headers={"X-Node-Token": "nope"}).status_code == 401
    assert client.post("/api/nodes/agent/observed", json={"replicas": {}}).status_code == 401


def test_a_node_cannot_report_about_instances_of_another_node(client, setup):
    node, app = setup
    mine, my_token = node("d-mine")
    other, _ = node("d-other")
    app_id = app("d-shared-app")
    theirs = _replica(app_id, other, 9109, status="starting", desired="running")
    spec_hash = _start(other, theirs)
    r = client.post("/api/nodes/agent/observed", headers={"X-Node-Token": my_token},
                    json={"replicas": {str(theirs): {"state": "error", "spec_hash": spec_hash, "error": "boom"}}})
    assert r.status_code == 200 and _row(theirs).status == "starting"


def test_reports_are_limited_in_size(client, setup):
    node, _ = setup
    _, token = node("d-big")
    r = client.post("/api/nodes/agent/observed", headers={"X-Node-Token": token},
                    json={"replicas": {"1": {"state": "error", "error": "x" * 5000}}})
    assert r.status_code == 422


# ── a report becomes a status ──────────────────────────────────────────────────
def _report(node_id, replica_id, **report):
    from nodes import observed

    async def go():
        await observed.apply(node_id, {str(replica_id): report})
        await asyncio.sleep(0)
    _run(go())


@pytest.fixture
def instance(setup):
    """An instance placed on a node that is on the node network, started with a spec."""
    node, app = setup

    def make(name, status="starting", port=9200):
        node_id, token = node(name)
        rid = _replica(app(f"{name}-app"), node_id, port, status=status)
        digest = _start(node_id, rid)
        return node_id, rid, digest
    return make


def test_a_container_that_runs_is_starting_until_it_can_be_reached(instance, monkeypatch):
    node_id, rid, digest = instance("o-run")
    started = []
    from nodes import promote
    monkeypatch.setattr(promote, "start_promotion", started.append)          # the check itself is tested in test_wireguard_mesh
    _report(node_id, rid, state="running", spec_hash=digest, container_id="c1")
    row = _row(rid)
    assert row.status == "starting" and row.container_id == "c1" and started == [rid]


def test_progress_is_shown_while_the_node_builds(instance):
    node_id, rid, digest = instance("o-build")
    _report(node_id, rid, state="building", substatus="building_image", spec_hash=digest)
    row = _row(rid)
    assert row.status == "starting" and row.substatus == "building_image"


def test_an_error_from_the_node_is_shown(instance):
    node_id, rid, digest = instance("o-error")
    _report(node_id, rid, state="error", error="docker build failed: no space left", spec_hash=digest)
    row = _row(rid)
    assert row.status == "error" and "no space left" in row.last_error


def test_a_report_about_an_older_start_is_ignored(instance):
    node_id, rid, digest = instance("o-stale")
    _report(node_id, rid, state="error", error="old failure", spec_hash="deadbeefdeadbeef")
    assert _row(rid).status == "starting" and _row(rid).last_error is None


def test_a_running_instance_that_crashes_leaves_nginx_and_shows_the_error(instance, monkeypatch):
    node_id, rid, digest = instance("o-crash", status="running")
    rewritten = []

    async def fake_regen(app_id):
        rewritten.append(app_id)
    from nodes import observed
    monkeypatch.setattr(observed.promote, "regen_nginx_for_app", fake_regen)
    _report(node_id, rid, state="error", error="the app keeps crashing", spec_hash=digest)
    assert _row(rid).status == "error" and rewritten == [_row(rid).app_id]


def test_a_node_that_comes_back_brings_its_instances_back(instance, monkeypatch):
    """Offline instances become starting again as soon as the node reports them, with nothing to recover."""
    node_id, rid, digest = instance("o-back", status="node_offline")
    from nodes import observed
    monkeypatch.setattr(observed.promote, "start_promotion", lambda replica_id: None)
    _report(node_id, rid, state="running", spec_hash=digest, container_id="c9")
    assert _row(rid).status == "starting"
    _report(node_id, rid, state="starting", substatus="creating_container", spec_hash=digest)
    assert _row(rid).status == "starting"


def test_a_stopped_instance_is_marked_stopped_when_the_node_says_so(instance):
    node_id, rid, _ = instance("o-stopped", status="stopping")
    _stop(node_id, rid)
    _report(node_id, rid, state="stopped")
    assert _row(rid).status == "stopped" and _row(rid).container_id is None


def test_an_instance_that_should_stop_is_not_revived_by_a_late_report(instance):
    node_id, rid, digest = instance("o-late", status="stopping")
    _stop(node_id, rid)
    _report(node_id, rid, state="running", spec_hash=digest)
    assert _row(rid).status == "stopping"


# ── the places that used to send commands ─────────────────────────────────────────
def test_starting_a_remote_instance_writes_the_list_instead_of_sending_a_command(setup):
    from core.database import AsyncSessionLocal
    from core.models import Application, ApplicationReplica, Node, NodeCommand
    from sqlalchemy import select
    from apps import runtime
    node, app = setup
    node_id, _ = node("c-start")
    app_id = app("c-start-app")
    rid = _replica(app_id, node_id, 9301)

    async def go():
        async with AsyncSessionLocal() as db:
            a, r, n = await db.get(Application, app_id), await db.get(ApplicationReplica, rid), await db.get(Node, node_id)
            await runtime._start_replica_runtime(a, r, n, {"A": "1"}, db)
            await db.commit()
            return (await db.execute(select(NodeCommand).where(NodeCommand.node_id == node_id))).scalars().all()
    commands = _run(go())
    row = _row(rid)
    assert commands == [] and row.status == "starting" and row.desired == "running" and row.spec_hash


def test_stopping_a_remote_instance_changes_the_list(setup):
    from core.database import AsyncSessionLocal
    from core.models import Application, ApplicationReplica, Node
    from apps import runtime
    node, app = setup
    node_id, _ = node("c-stop")
    app_id = app("c-stop-app")
    rid = _replica(app_id, node_id, 9302, status="running")
    _start(node_id, rid)

    async def go():
        async with AsyncSessionLocal() as db:
            a, r, n = await db.get(Application, app_id), await db.get(ApplicationReplica, rid), await db.get(Node, node_id)
            await runtime._stop_replica_runtime(a, r, n, db)
            await db.commit()
    _run(go())
    row = _row(rid)
    assert row.desired == "stopped" and row.status == "stopping"


def test_deleting_an_instance_needs_no_message_to_the_node(admin, setup):
    from core.models import NodeCommand
    from core.database import AsyncSessionLocal
    from sqlalchemy import select
    node, app = setup
    node_id, _ = node("c-delete")
    app_id = app("c-delete-app")
    rid = _replica(app_id, node_id, 9303, status="running")
    _start(node_id, rid)
    assert admin.delete(f"/api/apps/{app_id}/instances/{rid}").status_code == 200
    assert _row(rid) is None
    assert _snapshot(node_id)["replicas"] == []                     # gone from the list: the node removes the container

    async def commands():
        async with AsyncSessionLocal() as db:
            return (await db.execute(select(NodeCommand).where(NodeCommand.node_id == node_id))).scalars().all()
    assert _run(commands()) == []


def test_several_instances_can_be_removed_in_one_request(admin, setup):
    node, app = setup
    node_id, _ = node("c-bulk")
    app_id = app("c-bulk-app")
    ids = [_replica(app_id, node_id, 9310 + i, status="running") for i in range(3)]
    for rid in ids:
        _start(node_id, rid)
    assert admin.post(f"/api/apps/{app_id}/instances/remove", json={"ids": [ids[0], ids[1]]}).status_code == 200
    assert _row(ids[0]) is None and _row(ids[1]) is None and _row(ids[2]) is not None
    # An id that is not this app's instance removes nothing at all
    assert admin.post(f"/api/apps/{app_id}/instances/remove", json={"ids": [ids[2], 999999]}).status_code == 404
    assert _row(ids[2]) is not None


def test_scaling_to_a_wanted_number_removes_the_newest_instances(admin, setup):
    node, app = setup
    node_id, _ = node("c-target")
    app_id = app("c-target-app")
    ids = [_replica(app_id, node_id, 9320 + i, status="running") for i in range(4)]
    for rid in ids:
        _start(node_id, rid)
    assert admin.post(f"/api/apps/{app_id}/scale", json={"target": 4}).json()["removed"] == []
    body = admin.post(f"/api/apps/{app_id}/scale", json={"target": 2}).json()
    assert body["removed"] == ids[2:]
    assert _row(ids[0]) is not None and _row(ids[1]) is not None and _row(ids[2]) is None and _row(ids[3]) is None


def test_several_instances_can_be_restarted_in_one_request(admin, setup):
    node, app = setup
    node_id, _ = node("c-restart-many")
    app_id = app("c-restart-many-app")
    ids = [_replica(app_id, node_id, 9330 + i, status="running") for i in range(2)]
    for rid in ids:
        _start(node_id, rid)
    assert admin.post(f"/api/apps/{app_id}/instances/restart", json={"ids": ids}).status_code == 200
    assert admin.post(f"/api/apps/{app_id}/instances/restart", json={"ids": [999999]}).status_code == 404


def test_no_command_for_starting_or_stopping_is_left_in_the_code():
    from pathlib import Path
    backend = Path(__file__).resolve().parent.parent / "backend"
    for path in list(backend.rglob("*.py")):
        if "venv" in path.parts or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for gone in ('command_type="start_replica"', 'command_type="stop_replica"', 'command_type="refresh_source"',
                     'command_type="delete_app"', '"start_replica":', '"stop_replica":'):
            assert gone not in text, f"{path.name} still uses {gone}"


# ── commands that remain ────────────────────────────────────────────────────────
def test_a_command_that_was_never_answered_is_not_reused_for_ever(client, setup):
    from datetime import timedelta
    from core.database import AsyncSessionLocal
    from core.models import NodeCommand
    from nodes import commands
    node, _ = setup
    node_id, _ = node("k-dedupe")

    async def go():
        async with AsyncSessionLocal() as db:
            first = await commands.queue_node_command(db, node_id=node_id, command_type="get_stats", payload={}, app_id=None,
                                                      allow_existing_inflight=True)
            same = await commands.queue_node_command(db, node_id=node_id, command_type="get_stats", payload={}, app_id=None,
                                                     allow_existing_inflight=True)
            first_row = await db.get(NodeCommand, first.id)
            first_row.created_at = commands._utcnow() - timedelta(seconds=commands.INFLIGHT_REUSE_SECONDS + 5)
            await db.commit()
            later = await commands.queue_node_command(db, node_id=node_id, command_type="get_stats", payload={}, app_id=None,
                                                      allow_existing_inflight=True)
            return first.id, same.id, later.id
    first, same, later = _run(go())
    assert same == first and later != first


def test_a_new_agent_process_fails_what_the_old_one_was_doing(client, setup):
    from core.database import AsyncSessionLocal
    from core.models import NodeCommand
    from nodes import commands
    node, _ = setup
    node_id, _ = node("k-session")

    async def go():
        async with AsyncSessionLocal() as db:
            stuck = await commands.queue_node_command(db, node_id=node_id, command_type="get_replica_stats", payload={})
            cron = await commands.queue_node_command(db, node_id=node_id, command_type="cron_run", payload={})
            for c in (stuck, cron):
                (await db.get(NodeCommand, c.id)).status = "in_progress"
            await db.commit()
        await commands.fail_commands_in_flight(node_id)
        async with AsyncSessionLocal() as db:
            return (await db.get(NodeCommand, stuck.id)).status, (await db.get(NodeCommand, cron.id)).status
    assert _run(go()) == ("failed", "in_progress")                  # a cron job outlives a restart


def test_the_agent_websocket_tells_the_node_when_its_list_changed(admin, setup):
    node, app = setup
    node_id, token = node("k-ws")
    app_id = app("k-ws-app")
    rid = _replica(app_id, node_id, 9304, status="running")
    _start(node_id, rid)
    with admin.websocket_connect("/api/nodes/ws/agent") as ws:
        ws.send_json({"type": "auth", "token": token, "session": "s1"})
        assert ws.receive_json()["type"] == "auth_ok"
        assert ws.receive_json()["type"] == "sync"              # right after connecting: look at the list
        assert admin.delete(f"/api/apps/{app_id}/instances/{rid}").status_code == 200
        assert ws.receive_json()["type"] == "sync"              # the list changed: look again


# ── nodes that still run an older agent ──────────────────────────────────────────
def test_a_node_with_an_old_agent_is_not_given_work_it_would_never_see(setup):
    from fastapi import HTTPException
    from core.database import AsyncSessionLocal
    from core.models import Node
    node, app = setup
    node_id, _ = node("v-old")
    rid = _replica(app("v-old-app"), node_id, 9401)

    async def set_version(version):
        async with AsyncSessionLocal() as db:
            (await db.get(Node, node_id)).agent_version = version
            await db.commit()
    _run(set_version("1.1.0"))
    with pytest.raises(HTTPException, match="older Cloudbase"):
        _start(node_id, rid)
    assert _row(rid).desired is None                                  # nothing was written
    _run(set_version(None))                                            # not known yet: let it through
    assert _start(node_id, rid)
    _run(set_version("2.3.1"))
    assert _start(node_id, rid)
