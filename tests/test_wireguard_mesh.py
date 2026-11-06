"""The node network end to end on the primary: enrolling nodes, following their health, routing to instances."""
import asyncio
import base64
import json
import socket
import sys

import pytest

KEYS = [base64.b64encode(bytes([i]) * 32).decode() for i in range(1, 6)]


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def fake_hub(tmp_path, monkeypatch):
    """A stand-in for the root helper that records what the primary asks of it."""
    state = tmp_path / "hub.json"
    state.write_text(json.dumps({"calls": [], "peers": [], "handshakes": {}, "fail": False}))
    script = tmp_path / "fake_helper.py"
    script.write_text(
        "import json, sys\n"
        f"P = {str(state)!r}\n"
        "s = json.load(open(P))\n"
        "cmd = sys.argv[1]\n"
        "s['calls'].append(sys.argv[1:])\n"
        "if s['fail']:\n    json.dump(s, open(P, 'w')); print('no wireguard here', file=sys.stderr); sys.exit(1)\n"
        "if cmd == 'hub':\n    out = {'public_key': 'HUBKEY' + 'A' * 37 + '='}\n"
        "elif cmd == 'peers':\n    s['peers'] = json.load(sys.stdin); out = {'peers': len(s['peers'])}\n"
        "elif cmd == 'status':\n"
        "    out = {'up': True, 'peers': [{'public_key': k, 'latest_handshake': t} for k, t in s['handshakes'].items()]}\n"
        "else:\n    out = {}\n"
        "json.dump(s, open(P, 'w')); print(json.dumps(out))\n")
    monkeypatch.setenv("CLOUDBASE_WG_HELPER", f"{sys.executable} {script}")
    from mesh import hub
    monkeypatch.setitem(hub._hub, "up", False)
    monkeypatch.setitem(hub._hub, "error", None)

    class Fake:
        def read(self):
            return json.loads(state.read_text())

        def write(self, **changes):
            data = self.read()
            data.update(changes)
            state.write_text(json.dumps(data))
    return Fake()


def _add_node(name: str, key: str | None = None, ip: str | None = None, wg_state: str | None = None) -> tuple[int, str]:
    from core.database import AsyncSessionLocal
    from core.models import Node

    async def go():
        async with AsyncSessionLocal() as db:
            node = Node(name=name, role="node", status="online", auth_token=f"token-{name}", is_local=False, enabled=True,
                        wg_public_key=key, wg_ip=ip, wg_state=wg_state)
            db.add(node)
            await db.commit()
            return node.id, node.auth_token
    return _run(go())


def _delete_nodes(*ids):
    from sqlalchemy import delete
    from core.database import AsyncSessionLocal
    from core.models import ApplicationReplica, Node

    async def go():
        async with AsyncSessionLocal() as db:
            await db.execute(delete(ApplicationReplica).where(ApplicationReplica.node_id.in_(ids)))
            await db.execute(delete(Node).where(Node.id.in_(ids)))
            await db.commit()
    _run(go())


@pytest.fixture
def nodes(client):
    made = []

    def make(*args, **kwargs):
        node_id, token = _add_node(*args, **kwargs)
        made.append(node_id)
        return node_id, token
    yield make
    _delete_nodes(*made)


def _node(node_id):
    from core.database import AsyncSessionLocal
    from core.models import Node

    async def go():
        async with AsyncSessionLocal() as db:
            return await db.get(Node, node_id)
    return _run(go())


def _enroll(client, token, key):
    return client.post("/api/nodes/agent/wireguard", json={"public_key": key}, headers={"X-Node-Token": token})


# ── enrolling ─────────────────────────────────────────────────────────────────
def test_a_node_gets_an_address_and_the_hub_learns_about_it(client, fake_hub, nodes):
    a, token_a = nodes("wg-a")
    b, token_b = nodes("wg-b")
    ra = _enroll(client, token_a, KEYS[0])
    assert ra.status_code == 200, ra.text
    answer = ra.json()
    assert answer["address"] == "10.77.0.2/32" and answer["allowed"] == "10.77.0.0/16" and answer["port"] == 51820
    assert answer["hub_public_key"].startswith("HUBKEY")
    assert _enroll(client, token_b, KEYS[1]).json()["address"] == "10.77.0.3/32"
    assert fake_hub.read()["peers"] == [{"public_key": KEYS[0], "ip": "10.77.0.2"}, {"public_key": KEYS[1], "ip": "10.77.0.3"}]
    assert ["hub", "10.77.0.1/16", "51820"] in fake_hub.read()["calls"]


def test_enrolling_twice_keeps_the_same_address(client, fake_hub, nodes):
    _, token = nodes("wg-again")
    first = _enroll(client, token, KEYS[0]).json()["address"]
    assert _enroll(client, token, KEYS[0]).json()["address"] == first


def test_a_key_cannot_be_used_by_two_nodes(client, fake_hub, nodes):
    _, token_a = nodes("wg-k1")
    _, token_b = nodes("wg-k2")
    assert _enroll(client, token_a, KEYS[0]).status_code == 200
    assert _enroll(client, token_b, KEYS[0]).status_code == 409


def test_a_malformed_key_is_refused(client, fake_hub, nodes):
    _, token = nodes("wg-bad")
    assert _enroll(client, token, "x" * 44).status_code == 400
    assert _enroll(client, token, "short").status_code == 422
    assert fake_hub.read()["calls"] == []


def test_without_a_node_token_there_is_no_enrolment(client, fake_hub):
    assert client.post("/api/nodes/agent/wireguard", json={"public_key": KEYS[0]}).status_code == 401
    assert client.post("/api/nodes/agent/wireguard", json={"public_key": KEYS[0]}, headers={"X-Node-Token": "nope"}).status_code == 401


def test_enrolment_reports_a_hub_that_cannot_start(client, fake_hub, nodes):
    _, token = nodes("wg-broken")
    fake_hub.write(fail=True)
    r = _enroll(client, token, KEYS[0])
    assert r.status_code == 503 and "no wireguard here" in r.json()["detail"]


def test_enrolment_is_refused_when_the_node_network_is_off(client, fake_hub, nodes, monkeypatch):
    from core import config
    monkeypatch.setitem(config._config.setdefault("wireguard", {}), "enabled", False)
    _, token = nodes("wg-off")
    assert _enroll(client, token, KEYS[0]).status_code == 409


def test_removing_a_node_removes_its_peer(admin, fake_hub, nodes):
    from mesh import hub
    node_id, token = nodes("wg-gone")
    assert _enroll(admin, token, KEYS[2]).status_code == 200
    assert any(p["public_key"] == KEYS[2] for p in fake_hub.read()["peers"])
    assert admin.delete(f"/api/nodes/{node_id}").status_code == 200
    assert hub._hub["up"] and all(p["public_key"] != KEYS[2] for p in fake_hub.read()["peers"])


def test_the_panel_can_ask_about_the_node_network(admin, fake_hub):
    info = admin.get("/api/nodes/mesh").json()
    assert info["enabled"] is True and info["subnet"] == "10.77.0.0/16" and info["port"] == 51820


# ── health ────────────────────────────────────────────────────────────────────
def test_handshake_age_decides_the_state():
    from mesh.hub import HANDSHAKE_TIMEOUT, state_from_handshake
    assert state_from_handshake(1000, 1000 + 30) == "up"
    assert state_from_handshake(1000, 1000 + HANDSHAKE_TIMEOUT + 1) == "down"
    assert state_from_handshake(0, 5000) == "down"


def test_refresh_stores_states_and_reports_only_changes(client, fake_hub, nodes):
    import time
    from mesh import hub
    node_id, _ = nodes("wg-health", key=KEYS[3], ip="10.77.0.9")
    fake_hub.write(handshakes={KEYS[3]: int(time.time()) - 5})
    assert (node_id, None, "up") in _run(hub.refresh_states())
    assert _node(node_id).wg_state == "up" and _node(node_id).wg_handshake_at is not None
    assert not [c for c in _run(hub.refresh_states()) if c[0] == node_id]
    fake_hub.write(handshakes={KEYS[3]: int(time.time()) - 1000})
    assert (node_id, "up", "down") in _run(hub.refresh_states())


def test_a_helper_that_fails_means_every_node_is_down(client, fake_hub, nodes):
    import time
    from mesh import hub
    node_id, _ = nodes("wg-helper-down", key=KEYS[3], ip="10.77.0.9", wg_state="up")
    fake_hub.write(fail=True, handshakes={KEYS[3]: int(time.time())})
    assert (node_id, "up", "down") in _run(hub.refresh_states())
    assert hub._hub["up"] is False


# ── routing ───────────────────────────────────────────────────────────────────
def _replica(node_id, port, status="starting", app_id=None):
    from core.database import AsyncSessionLocal
    from core.models import Application, ApplicationReplica

    async def go():
        async with AsyncSessionLocal() as db:
            aid = app_id
            if aid is None:
                app = Application(name=f"mesh-app-{port}", repo_url="https://example.test/x", start_command="x", port=3000,
                                  external_port=port, status="running")
                db.add(app)
                await db.flush()
                aid = app.id
            rep = ApplicationReplica(app_id=aid, node_id=node_id, external_port=port, status=status)
            db.add(rep)
            await db.commit()
            return rep.id, aid
    return _run(go())


def _replica_row(replica_id):
    from core.database import AsyncSessionLocal
    from core.models import ApplicationReplica

    async def go():
        async with AsyncSessionLocal() as db:
            return await db.get(ApplicationReplica, replica_id)
    return _run(go())


@pytest.fixture
def listener():
    """A port that accepts connections, standing in for an instance reached over the node network."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(5)
    yield sock.getsockname()[1]
    sock.close()


def test_the_backend_is_local_for_the_primary_and_over_the_node_network_for_others():
    from core.models import ApplicationReplica, Node
    from mesh.routing import backend_for
    rep = ApplicationReplica(external_port=8123)
    assert backend_for(rep, None) == "127.0.0.1:8123"
    assert backend_for(rep, Node(is_local=True)) == "127.0.0.1:8123"
    assert backend_for(rep, Node(is_local=False, wg_ip="10.77.0.4")) == "10.77.0.4:8123"
    assert backend_for(rep, Node(is_local=False, wg_ip=None)) is None          # not on the node network: no address
    assert backend_for(ApplicationReplica(external_port=None), Node(is_local=False, wg_ip="10.77.0.4")) is None


def test_nginx_only_gets_instances_that_are_reachable_over_the_node_network(client, nodes):
    from core.database import AsyncSessionLocal
    from core.models import Application
    from apps.nginx_sync import _get_nginx_backends
    on_wg, _ = nodes("wg-nginx-a", key=KEYS[3], ip="10.77.0.20", wg_state="up")
    off_wg, _ = nodes("wg-nginx-b")
    rid1, app_id = _replica(on_wg, 9301, status="running")
    _replica(off_wg, 9302, status="running", app_id=app_id)
    _replica(on_wg, 9303, status="starting", app_id=app_id)

    async def go():
        async with AsyncSessionLocal() as db:
            return await _get_nginx_backends(await db.get(Application, app_id), db)
    assert _run(go()) == ["10.77.0.20:9301"]
    assert rid1


def test_waiting_for_an_instance_returns_its_backend(client, nodes, listener):
    from mesh import routing
    node_id, _ = nodes("wg-wait", ip="127.0.0.1")
    rid, _ = _replica(node_id, listener)
    assert _run(routing.wait_reachable(rid, 5)) == f"127.0.0.1:{listener}"


def test_waiting_stops_at_once_when_the_node_reports_an_error(client, nodes):
    from mesh import routing
    node_id, _ = nodes("wg-wait-err", ip="127.0.0.1")
    rid, _ = _replica(node_id, 9399, status="error")
    with pytest.raises(routing.Unreachable, match="node reported an error"):
        _run(routing.wait_reachable(rid, 5))


def test_waiting_gives_up_after_the_timeout(client, nodes, monkeypatch):
    from mesh import routing
    monkeypatch.setattr(routing, "POLL_SECONDS", 0.05)
    node_id, _ = nodes("wg-wait-late", ip="127.0.0.1")
    rid, _ = _replica(node_id, 9398)
    with pytest.raises(routing.Unreachable, match="did not become reachable"):
        _run(routing.wait_reachable(rid, 0.2))


def test_waiting_notices_an_instance_that_was_removed(client):
    from mesh import routing
    with pytest.raises(routing.Unreachable, match="removed"):
        _run(routing.wait_reachable(987654, 1))


# ── going live ────────────────────────────────────────────────────────────────
def test_a_started_instance_goes_live_when_it_can_be_reached(client, nodes, listener):
    from nodes import promote
    node_id, _ = nodes("wg-live", ip="127.0.0.1", wg_state="up")
    rid, _ = _replica(node_id, listener)
    _run(promote.promote_when_reachable(rid))
    row = _replica_row(rid)
    assert row.status == "running" and row.last_error is None


def test_an_instance_that_a_deploy_already_handled_is_left_alone(client, nodes, listener):
    from nodes import promote
    node_id, _ = nodes("wg-live-stopped", ip="127.0.0.1", wg_state="up")
    rid, _ = _replica(node_id, listener, status="stopped")
    _run(promote.promote_when_reachable(rid))
    assert _replica_row(rid).status == "stopped"


def test_an_unreachable_instance_waits_for_recovery_when_the_link_is_down_and_fails_otherwise(client, nodes, monkeypatch):
    from mesh import routing
    from nodes import promote
    monkeypatch.setattr(promote, "REACHABLE_TIMEOUT", 0.1)
    monkeypatch.setattr(routing, "POLL_SECONDS", 0.05)
    down, _ = nodes("wg-late-down", ip="127.0.0.1", wg_state="down")
    up, _ = nodes("wg-late-up", ip="127.0.0.1", wg_state="up")
    r_down, _ = _replica(down, 9397)
    r_up, _ = _replica(up, 9396)
    _run(promote.promote_when_reachable(r_down))
    _run(promote.promote_when_reachable(r_up))
    assert _replica_row(r_down).status == "node_offline"
    assert _replica_row(r_up).status == "error" and "did not become reachable" in _replica_row(r_up).last_error


def test_a_broken_link_takes_the_instances_out_and_a_restored_one_brings_them_back(client, nodes):
    from nodes import promote
    from nodes.recovery import recover_node_replicas
    from core.database import AsyncSessionLocal
    from core.models import NodeCommand
    from sqlalchemy import select
    node_id, _ = nodes("wg-flap", ip="127.0.0.1", wg_state="up")
    rid, _ = _replica(node_id, 9395, status="running")
    _run(promote.link_down(node_id))
    assert _replica_row(rid).status == "node_offline"

    async def queued():
        async with AsyncSessionLocal() as db:
            return (await db.execute(select(NodeCommand).where(NodeCommand.node_id == node_id))).scalars().all()

    async def set_state(value):
        async with AsyncSessionLocal() as db:
            from core.models import Node
            (await db.get(Node, node_id)).wg_state = value
            await db.commit()

    _run(set_state("down"))
    _run(recover_node_replicas(node_id))                        # link still down: nothing is started
    assert _run(queued()) == [] and _replica_row(rid).status == "node_offline"


def test_promotion_is_wired_to_started_instances():
    from nodes import commands, promote
    assert promote.start_promotion in commands._replica_started_hooks


# ── the agent ────────────────────────────────────────────────────────────────
class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class _FakeClient:
    def __init__(self):
        self.posted = []

    async def post(self, url, json=None, **kw):
        self.posted.append((url, json))
        return _FakeResponse({"container_id": "c1"})


def test_the_agent_publishes_instances_on_its_node_network_address_only(monkeypatch):
    from agent import source
    from agent.commands import apps as agent_apps
    from agent.state import AgentState

    async def noop(*a, **k):
        return 7

    for name in ("_wait_local_api", "_report_replica_substatus"):
        monkeypatch.setattr(agent_apps, name, noop)
    monkeypatch.setattr(agent_apps, "_ensure_replica_app_deployed", noop)
    monkeypatch.setattr(agent_apps.containers_replicas, "is_replica_container_running", lambda *a: False)
    assert source
    client = _FakeClient()
    payload = {"replica_id": 5, "app_name": "x", "external_port": 8123, "docker_options": {"bind_host": "127.0.0.1", "restart_policy": "no"}}

    state = AgentState(main_url="http://p", auth_token="t", node_id=1, node_name="n", wg_ip="10.77.0.5")
    _run(agent_apps.cmd_start_replica(client, state, 1, payload, {}))
    body = client.posted[0][1]
    assert body["docker_options"]["bind_host"] == "10.77.0.5" and body["docker_options"]["restart_policy"] == "no"


def test_the_agent_refuses_to_start_instances_before_it_is_on_the_node_network():
    from agent.commands import apps as agent_apps
    from agent.state import AgentState
    state = AgentState(main_url="http://p", auth_token="t", node_id=1, node_name="n")
    with pytest.raises(RuntimeError, match="node network"):
        _run(agent_apps.cmd_start_replica(_FakeClient(), state, 1, {"replica_id": 5, "external_port": 8123}, {}))


def test_the_agent_joins_with_the_hub_the_primary_describes(monkeypatch):
    from agent import mesh as agent_mesh
    from agent.state import AgentState
    from mesh import helper
    started = []
    monkeypatch.setattr(helper, "keygen", lambda: KEYS[4])
    monkeypatch.setattr(helper, "start_node", lambda *args: started.append(args) or KEYS[4])
    monkeypatch.setattr(agent_mesh, "_save_state", lambda s: None)

    class Client:
        async def post(self, url, json=None, headers=None, timeout=None):
            assert url == "http://pi.example.test:7823/api/nodes/agent/wireguard" and json == {"public_key": KEYS[4]}
            return _FakeResponse({"address": "10.77.0.7/32", "allowed": "10.77.0.0/16", "hub_public_key": KEYS[0], "port": 51820, "endpoint": ""})

    state = AgentState(main_url="http://pi.example.test:7823", auth_token="t", node_id=1, node_name="n")
    _run(agent_mesh.join_once(Client(), state))
    assert started == [("10.77.0.7/32", KEYS[0], "pi.example.test:51820", "10.77.0.0/16")] and state.wg_ip == "10.77.0.7"


def test_the_configured_endpoint_wins_over_the_address_the_agent_uses():
    from agent.mesh import _endpoint
    from agent.state import AgentState
    state = AgentState(main_url="https://panel.example.test", auth_token="t", node_id=1, node_name="n")
    assert _endpoint(state, {"endpoint": "vpn.example.test", "port": 51820}) == "vpn.example.test:51820"
    assert _endpoint(state, {"endpoint": "203.0.113.5:4000", "port": 51820}) == "203.0.113.5:4000"
    assert _endpoint(state, {"endpoint": "", "port": 51820}) == "panel.example.test:51820"
