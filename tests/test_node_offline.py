"""What "a node is offline" means is decided in one place (nodes/liveness.py): the node is marked, and its
live instances leave rotation until it reports them again."""
import asyncio
import time
from datetime import datetime, timedelta

import pytest


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def world(client):
    """Nodes, an app and instances, removed again afterwards."""
    from sqlalchemy import delete
    from core.database import AsyncSessionLocal
    from core.models import Application, ApplicationReplica, Node, NodeCommand
    made = {"nodes": [], "apps": []}

    class World:
        def node(self, name, **kw):
            async def go():
                async with AsyncSessionLocal() as db:
                    node = Node(name=name, role="node", status=kw.pop("status", "online"), auth_token=f"token-{name}", is_local=False,
                                enabled=True, agent_version="2.0.0", last_seen=datetime.utcnow(), **kw)
                    db.add(node)
                    await db.commit()
                    return node.id
            node_id = _run(go())
            made["nodes"].append(node_id)
            return node_id

        def app(self, name):
            async def go():
                async with AsyncSessionLocal() as db:
                    app = Application(name=name, repo_url="https://example.test/x", start_command="npm start", port=3000)
                    db.add(app)
                    await db.commit()
                    return app.id
            app_id = _run(go())
            made["apps"].append(app_id)
            return app_id

        def replica(self, app_id, node_id, status, port):
            async def go():
                async with AsyncSessionLocal() as db:
                    row = ApplicationReplica(app_id=app_id, node_id=node_id, external_port=port, status=status, substatus="waiting")
                    db.add(row)
                    await db.commit()
                    return row.id
            return _run(go())

        def replica_row(self, replica_id):
            async def go():
                async with AsyncSessionLocal() as db:
                    return await db.get(ApplicationReplica, replica_id)
            return _run(go())

        def node_row(self, node_id):
            async def go():
                async with AsyncSessionLocal() as db:
                    return await db.get(Node, node_id)
            return _run(go())

    yield World()

    async def cleanup():
        async with AsyncSessionLocal() as db:
            await db.execute(delete(NodeCommand).where(NodeCommand.node_id.in_(made["nodes"])))
            await db.execute(delete(ApplicationReplica).where(ApplicationReplica.app_id.in_(made["apps"])))
            await db.execute(delete(Application).where(Application.id.in_(made["apps"])))
            await db.execute(delete(Node).where(Node.id.in_(made["nodes"])))
            await db.commit()
    _run(cleanup())


@pytest.fixture
def events(monkeypatch):
    """The 'node_offline' events the panel was told about."""
    from nodes import liveness
    seen = []
    monkeypatch.setattr(liveness, "_push_node_event", lambda node_id, event: seen.append((node_id, event["type"])))
    return seen


def test_going_offline_takes_the_live_instances_out_of_rotation_and_nothing_else(world, events):
    from nodes import liveness
    gone, other = world.node("off-gone"), world.node("off-other")
    app = world.app("off-app")
    live = world.replica(app, gone, "running", 9501)
    booting = world.replica(app, gone, "starting", 9502)
    stopped = world.replica(app, gone, "stopped", 9503)
    broken = world.replica(app, gone, "error", 9504)
    elsewhere = world.replica(app, other, "running", 9505)

    _run(liveness.node_gone(gone))

    node = world.node_row(gone)
    assert node.status == "offline" and node.offline_since is not None and node.connection_type is None
    assert world.replica_row(live).status == "node_offline" and world.replica_row(live).substatus is None
    assert world.replica_row(booting).status == "node_offline"
    assert world.replica_row(stopped).status == "stopped"
    assert world.replica_row(broken).status == "error"
    assert world.replica_row(elsewhere).status == "running"          # another node is not affected
    assert world.node_row(other).status == "online"
    assert events == [(gone, "node_offline")]


def test_going_offline_twice_changes_nothing_the_second_time(world):
    from nodes import liveness
    node_id = world.node("off-twice")
    app = world.app("off-twice-app")
    _run(liveness.node_gone(node_id))
    since = world.node_row(node_id).offline_since
    back = world.replica(app, node_id, "running", 9511)               # came back to life by itself (e.g. a late report)
    _run(liveness.node_gone(node_id))
    assert world.node_row(node_id).offline_since == since
    assert world.replica_row(back).status == "running"                # the node was already offline: nothing to redo


def test_a_node_that_went_silent_is_marked_offline_and_a_chatty_one_is_not(world, events):
    from core.database import AsyncSessionLocal
    from nodes import liveness
    silent, chatty = world.node("off-silent"), world.node("off-chatty")
    app = world.app("off-silent-app")
    on_silent = world.replica(app, silent, "running", 9521)
    on_chatty = world.replica(app, chatty, "running", 9522)

    async def make_silent():
        from core.models import Node
        async with AsyncSessionLocal() as db:
            (await db.get(Node, silent)).last_seen = datetime.utcnow() - timedelta(minutes=5)
            await db.commit()
    _run(make_silent())

    async def go():
        async with AsyncSessionLocal() as db:
            await liveness.mark_stale_nodes_offline(db)
    _run(go())

    assert world.node_row(silent).status == "offline" and world.replica_row(on_silent).status == "node_offline"
    assert world.node_row(chatty).status == "online" and world.replica_row(on_chatty).status == "running"
    assert (silent, "node_offline") in events and (chatty, "node_offline") not in events


def test_a_broken_node_network_takes_the_instances_out_of_rotation_but_not_the_node(world, monkeypatch):
    from nodes import promote
    node_id = world.node("off-link")
    app = world.app("off-link-app")
    live = world.replica(app, node_id, "running", 9531)
    stopped = world.replica(app, node_id, "stopped", 9532)
    rewritten = []

    async def fake_regen(app_id):
        rewritten.append(app_id)
    monkeypatch.setattr(promote, "regen_nginx_for_app", fake_regen)
    _run(promote.link_down(node_id))
    assert world.replica_row(live).status == "node_offline" and world.replica_row(stopped).status == "stopped"
    assert world.node_row(node_id).status == "online"
    assert rewritten == [app]


# ── the agent's websocket ─────────────────────────────────────────────────────
def _wait_for(check, seconds=8.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if check():
            return True
        time.sleep(0.1)
    return False


def _connect(admin, token):
    ws = admin.websocket_connect("/api/nodes/ws/agent")
    sock = ws.__enter__()
    sock.send_json({"type": "auth", "token": token, "session": "s1"})
    assert sock.receive_json()["type"] == "auth_ok"
    assert sock.receive_json()["type"] == "sync"
    return ws, sock


def test_a_closed_agent_connection_marks_the_node_offline(admin, world):
    node_id = world.node("ws-close")
    live = world.replica(world.app("ws-close-app"), node_id, "running", 9541)
    ws, sock = _connect(admin, "token-ws-close")
    try:
        assert world.node_row(node_id).status == "online"
        sock.close(1000)          # the agent hangs up (the test client cancels the handler if its own context is left first)
        assert _wait_for(lambda: world.node_row(node_id).status == "offline")
        assert world.replica_row(live).status == "node_offline"
    finally:
        ws.__exit__(None, None, None)


def test_a_connection_the_panel_dropped_ends_and_the_node_is_marked_offline_once(admin, world):
    """A ping that times out evicts the connection: the handler must leave (no flapping online/offline)."""
    from nodes.state import _node_ws_connections
    node_id = world.node("ws-evict")
    live = world.replica(world.app("ws-evict-app"), node_id, "running", 9551)
    ws, sock = _connect(admin, "token-ws-evict")
    try:
        _node_ws_connections.pop(node_id, None)
        assert _wait_for(lambda: world.node_row(node_id).status == "offline")
        assert world.replica_row(live).status == "node_offline"
        time.sleep(4)                                               # longer than one round of the handler: it does not flap back
        assert world.node_row(node_id).status == "offline"
    finally:
        ws.__exit__(None, None, None)


def test_a_newer_connection_replaces_the_old_one_without_marking_the_node_offline(admin, world):
    node_id = world.node("ws-replace")
    live = world.replica(world.app("ws-replace-app"), node_id, "running", 9561)
    ws1, sock1 = _connect(admin, "token-ws-replace")
    ws2, sock2 = _connect(admin, "token-ws-replace")
    try:
        with pytest.raises(Exception):                              # the old one is closed by the panel
            for _ in range(5):
                sock1.receive_json()
        time.sleep(1.0)                                             # the old handler has cleaned up by now
        assert world.node_row(node_id).status == "online"
        assert world.replica_row(live).status == "running"
    finally:
        ws2.__exit__(None, None, None)
        ws1.__exit__(None, None, None)
