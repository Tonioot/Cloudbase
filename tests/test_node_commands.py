"""Questions to a node live in memory (nodes/commands.py); the protocol with the agent is what it always was:
the command goes out over the websocket, the answer comes back over HTTP."""
import asyncio
import threading

import pytest


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def node(client):
    from sqlalchemy import delete
    from core.database import AsyncSessionLocal
    from core.models import Node
    made = []

    def make(name):
        async def go():
            async with AsyncSessionLocal() as db:
                row = Node(name=name, status="online", auth_token=f"token-{name}", is_local=False, enabled=True,
                           agent_version="4.0.0")
                db.add(row)
                await db.commit()
                return row.id
        node_id = _run(go())
        made.append(node_id)
        return node_id, f"token-{name}"
    yield make

    async def cleanup():
        async with AsyncSessionLocal() as db:
            await db.execute(delete(Node).where(Node.id.in_(made)))
            await db.commit()
    _run(cleanup())


def _connect(admin, token):
    ws = admin.websocket_connect("/api/nodes/ws/agent")
    sock = ws.__enter__()
    sock.send_json({"type": "auth", "token": token, "session": "s1"})
    assert sock.receive_json()["type"] == "auth_ok"
    assert sock.receive_json()["type"] == "sync"
    return ws, sock


def _next_command(sock):
    while True:
        msg = sock.receive_json()
        if msg["type"] == "command":
            return msg["command"]


def _in_background(fn):
    out = {}

    def run():
        out["result"] = fn()
    t = threading.Thread(target=run)
    t.start()
    return t, out


def test_a_question_goes_out_over_the_websocket_and_the_answer_comes_back_over_http(admin, node):
    node_id, token = node("cmd-http")
    ws, sock = _connect(admin, token)
    try:
        t, out = _in_background(lambda: admin.get(f"/api/nodes/{node_id}/agent-logs?limit=5"))
        cmd = _next_command(sock)
        assert cmd["command_type"] == "get_agent_logs" and cmd["payload"] == {"limit": 5} and isinstance(cmd["id"], int)
        r = admin.post(f"/api/nodes/agent/commands/{cmd['id']}/result", headers={"X-Node-Token": token},
                       json={"status": "done", "result": {"lines": ["a", "b"]}})
        assert r.status_code == 200, r.text
        t.join(10)
        assert out["result"].json() == {"lines": ["a", "b"], "source": "remote"}
        history = admin.get(f"/api/nodes/{node_id}/commands").json()
        assert history[0]["command_type"] == "get_agent_logs" and history[0]["status"] == "done"
    finally:
        ws.__exit__(None, None, None)


def test_an_answer_nobody_waits_for_is_refused(admin, node):
    """E.g. for a question from before a restart of the panel."""
    node_id, token = node("cmd-unknown")
    r = admin.post("/api/nodes/agent/commands/123/result", headers={"X-Node-Token": token}, json={"status": "done"})
    assert r.status_code == 404


def test_another_nodes_answer_is_refused(admin, node):
    node_id, token = node("cmd-mine")
    _, other_token = node("cmd-other")
    ws, sock = _connect(admin, token)
    try:
        t, out = _in_background(lambda: admin.get(f"/api/nodes/{node_id}/agent-logs"))
        cmd = _next_command(sock)
        r = admin.post(f"/api/nodes/agent/commands/{cmd['id']}/result", headers={"X-Node-Token": other_token},
                       json={"status": "done", "result": {"lines": ["forged"]}})
        assert r.status_code == 404
        admin.post(f"/api/nodes/agent/commands/{cmd['id']}/result", headers={"X-Node-Token": token},
                   json={"status": "done", "result": {"lines": ["real"]}})
        t.join(10)
        assert out["result"].json()["lines"] == ["real"]
    finally:
        ws.__exit__(None, None, None)


def test_a_question_to_a_node_that_does_not_answer_times_out_and_is_forgotten(client, node, monkeypatch):
    from fastapi import HTTPException
    from nodes import commands
    node_id, _ = node("cmd-silent")

    async def go():
        with pytest.raises(HTTPException) as err:
            await commands.ask(node_id, "get_stats", {}, timeout=0.1)
        return err.value.status_code
    assert _run(go()) == 504
    assert not [c for c in commands._open.values() if c.node_id == node_id]
    assert commands.history(node_id)[0]["status"] == "failed"
