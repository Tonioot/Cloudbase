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
    from mesh import hub, publicip
    from workers import mesh as mesh_worker

    async def no_tick():
        return None
    monkeypatch.setattr(mesh_worker, "tick", no_tick)           # the app's own monitor must not race the test
    monkeypatch.setattr(publicip, "detect", lambda: "203.0.113.7")
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
    assert answer["endpoints"] == ["203.0.113.7"]                    # found by the primary itself, no domain needed
    assert _enroll(client, token_b, KEYS[1]).json()["address"] == "10.77.0.3/32"
    assert fake_hub.read()["peers"] == [{"public_key": KEYS[0], "ip": "10.77.0.2"}, {"public_key": KEYS[1], "ip": "10.77.0.3"}]
    assert ["hub", "10.77.0.1/16", "51820"] in fake_hub.read()["calls"]


def test_a_configured_address_is_used_instead_of_looking_one_up(client, fake_hub, nodes, monkeypatch):
    from core import config
    from mesh import publicip
    monkeypatch.setitem(config._config.setdefault("wireguard", {}), "endpoint", "vpn.example.test")
    monkeypatch.setattr(publicip, "detect", lambda: pytest.fail("must not look up the address"))
    _, token = nodes("wg-cfg")
    assert _enroll(client, token, KEYS[0]).json()["endpoints"] == ["vpn.example.test"]


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
                                  status="running")
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


def _agent_state():
    from agent.state import AgentState
    return AgentState(main_url="https://panel.example.test", auth_token="t", node_id=1, node_name="n")


def test_the_node_tries_the_primarys_address_first_and_the_panel_address_last():
    from agent.mesh import _candidates
    answer = {"endpoints": ["203.0.113.7"], "port": 51820}
    assert _candidates(_agent_state(), answer) == ["203.0.113.7:51820", "panel.example.test:51820"]
    assert _candidates(_agent_state(), {"endpoints": ["vpn.example.test:4000"], "port": 51820})[0] == "vpn.example.test:4000"
    assert _candidates(_agent_state(), {"endpoints": [], "port": 51820}) == ["panel.example.test:51820"]
    assert _candidates(_agent_state(), {"endpoints": ["panel.example.test"], "port": 51820}) == ["panel.example.test:51820"]   # no duplicates


class _JoinClient:
    async def post(self, url, json=None, headers=None, timeout=None):
        assert url == "https://panel.example.test/api/nodes/agent/wireguard" and json == {"public_key": KEYS[4]}
        return _FakeResponse({"address": "10.77.0.7/32", "allowed": "10.77.0.0/16", "hub_public_key": KEYS[0],
                              "port": 51820, "endpoints": ["203.0.113.7"]})


def _join_with(monkeypatch, answers_on: set):
    """Run a join where only the endpoints in answers_on respond with a handshake."""
    from agent import mesh as agent_mesh
    from mesh import helper
    started = []
    monkeypatch.setattr(helper, "keygen", lambda: KEYS[4])
    monkeypatch.setattr(helper, "start_node", lambda *args: started.append(args) or KEYS[4])
    monkeypatch.setattr(helper, "status", lambda: {"peers": [{"latest_handshake": 5 if started[-1][2] in answers_on else 0}]})
    monkeypatch.setattr(agent_mesh, "_save_state", lambda s: None)
    monkeypatch.setattr(agent_mesh, "HANDSHAKE_WAIT_SECONDS", 1)
    real_sleep = asyncio.sleep

    async def quick_sleep(seconds):
        await real_sleep(0)
    monkeypatch.setattr(agent_mesh.asyncio, "sleep", quick_sleep)
    state = _agent_state()
    return agent_mesh, state, started


def test_the_agent_stays_on_the_first_address_that_answers(monkeypatch):
    agent_mesh, state, started = _join_with(monkeypatch, {"203.0.113.7:51820"})
    _run(agent_mesh.join_once(_JoinClient(), state))
    assert [s[2] for s in started] == ["203.0.113.7:51820"] and state.wg_ip == "10.77.0.7"


def test_the_agent_falls_back_to_the_panel_address(monkeypatch):
    agent_mesh, state, started = _join_with(monkeypatch, {"panel.example.test:51820"})
    _run(agent_mesh.join_once(_JoinClient(), state))
    assert [s[2] for s in started] == ["203.0.113.7:51820", "panel.example.test:51820"]


def test_when_nothing_answers_the_error_says_what_to_forward(monkeypatch):
    agent_mesh, state, started = _join_with(monkeypatch, set())
    with pytest.raises(RuntimeError, match="Forward UDP port 51820"):
        _run(agent_mesh.join_once(_JoinClient(), state))
    assert state.wg_ip == "10.77.0.7"                    # the node already has its address, for when the hub does answer


def test_the_primary_finds_its_own_public_address(monkeypatch):
    from mesh import publicip
    monkeypatch.setattr(publicip, "_cache", {"ip": None, "at": 0.0})
    answers = iter(["10.0.0.5", "not an ip", "93.184.216.34"])       # private and garbage answers are ignored
    monkeypatch.setattr(publicip, "_fetch", lambda url: next(answers))
    monkeypatch.setattr(publicip, "SERVICES", ("a", "b", "c"))
    assert publicip.detect() == "93.184.216.34"
    monkeypatch.setattr(publicip, "_fetch", lambda url: pytest.fail("cached for an hour"))
    assert publicip.detect() == "93.184.216.34"


def test_without_any_answer_there_is_no_public_address(monkeypatch):
    from mesh import publicip
    monkeypatch.setattr(publicip, "_cache", {"ip": None, "at": 0.0})
    monkeypatch.setattr(publicip, "_fetch", lambda url: (_ for _ in ()).throw(OSError("offline")))
    assert publicip.detect() is None
