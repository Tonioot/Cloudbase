"""Databases across nodes go over the node network: a database publishes its port on its node's node-network address
only, and an app on another node (or the primary's public access) connects there. No tunnels through the primary."""
import asyncio
import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

from dbs import docker as dbs_docker, lifecycle as dbs_lifecycle, links as db_links, proxy as db_proxy


def test_an_app_gets_the_container_on_its_own_node_and_the_node_address_elsewhere(monkeypatch):
    monkeypatch.setattr(db_proxy, "local_node_id", lambda: 2)
    link = {"database_id": 7, "env_var": "DATABASE_URL", "direct_url": "pg://cloudbase-db-7:5432",
            "remote_url": "pg://10.77.0.3:20007"}
    assert db_proxy.resolve_links([{**link, "node_id": 2}]) == {"DATABASE_URL": "pg://cloudbase-db-7:5432"}
    assert db_proxy.resolve_links([{**link, "node_id": 3}]) == {"DATABASE_URL": "pg://10.77.0.3:20007"}
    # no node network: the direct URL is the best there is
    assert db_proxy.resolve_links([{**link, "node_id": 3, "remote_url": None}]) == {"DATABASE_URL": "pg://cloudbase-db-7:5432"}


def test_a_database_is_reached_at_its_nodes_address_on_the_node_network(monkeypatch):
    from core.models import Node
    from core import config
    monkeypatch.setattr(config, "get_wireguard", lambda: {"enabled": True, "subnet": "10.77.0.0/16"})
    assert db_links.node_address(Node(is_local=False, wg_ip="10.77.0.5")) == "10.77.0.5"
    assert db_links.node_address(Node(is_local=False, wg_ip=None)) is None
    assert db_links.node_address(Node(is_local=True)) == "10.77.0.1"          # the primary is the hub
    assert db_links.node_address(None) is None


class _Container:
    def __init__(self, binding=None, state="running"):
        ports = {"5432/tcp": [{"HostIp": binding[0], "HostPort": str(binding[1])}]} if binding else {}
        self.attrs = {"HostConfig": {"PortBindings": ports}, "State": {"Status": state}}
        self.id, self.removed, self.started = "c1", False, False

    def remove(self, force=False, v=False):
        self.removed = True

    def start(self):
        self.started = True


def _spec(db_id=7):
    return {"database_id": db_id, "engine": "postgres", "version": "16", "initialized": True, "volume_token": "t",
            "credentials": {"user": "u", "password": "p", "db_name": "d"}}


@pytest.fixture
def docker_here(monkeypatch):
    """A Docker with one existing container; records what would be created."""
    made = []
    state = {"container": None}
    monkeypatch.setattr(dbs_lifecycle, "_client", lambda: object())
    monkeypatch.setattr(dbs_lifecycle, "_get_container", lambda client, i: state["container"])
    monkeypatch.setattr(dbs_lifecycle, "_volume_exists", lambda client, i: True)
    monkeypatch.setattr(dbs_lifecycle, "_container_state", lambda c: "running")

    def run(client, spec):
        made.append(spec["database_id"])
        return _Container()
    monkeypatch.setattr(dbs_lifecycle, "_run_container", run)
    return state, made


def test_a_container_published_elsewhere_is_replaced_and_its_data_kept(docker_here, monkeypatch):
    state, made = docker_here
    port = dbs_docker.published_port(7)
    monkeypatch.setattr(dbs_lifecycle, "publish_address", lambda: "10.77.0.3")

    state["container"] = old = _Container(binding=None)                        # made before the node network
    dbs_lifecycle.ensure_running(_spec())
    assert old.removed and made == [7]

    made.clear()
    state["container"] = same = _Container(binding=("10.77.0.3", port))
    dbs_lifecycle.ensure_running(_spec())
    assert not same.removed and made == []


def test_without_a_node_network_address_the_container_is_left_alone(docker_here, monkeypatch):
    """At boot the network may come up after Docker: do not throw away a correct container meanwhile."""
    state, made = docker_here
    monkeypatch.setattr(dbs_lifecycle, "publish_address", lambda: None)
    state["container"] = c = _Container(binding=("10.77.0.3", dbs_docker.published_port(7)))
    dbs_lifecycle.ensure_running(_spec())
    assert not c.removed and made == []


def test_the_container_publishes_on_the_node_network_address_only(monkeypatch):
    runs = []

    class Containers:
        def run(self, image, **kwargs):
            runs.append(kwargs)
            return _Container()

    class Client:
        containers = Containers()

    import sys
    import types
    monkeypatch.setitem(sys.modules, "docker", sys.modules.get("docker") or types.ModuleType("docker"))
    monkeypatch.setattr(dbs_docker, "_ensure_image", lambda *a: None)
    monkeypatch.setattr(dbs_docker.containers_client, "ensure_database_network", lambda database_id: "net")
    monkeypatch.setattr(dbs_docker, "publish_address", lambda: "10.77.0.3")
    dbs_docker._run_container(Client(), _spec())
    assert runs[-1]["ports"] == {"5432/tcp": ("10.77.0.3", dbs_docker.published_port(7))}
    monkeypatch.setattr(dbs_docker, "publish_address", lambda: None)
    dbs_docker._run_container(Client(), _spec())
    assert "ports" not in runs[-1]                                              # never on every address


def test_a_node_publishes_on_its_own_address_and_the_primary_on_the_hub_when_it_runs(monkeypatch):
    from agent.state import AgentState
    from mesh import hub
    monkeypatch.setattr(dbs_docker.agent_state, "_load_state", lambda: AgentState(main_url="x", node_id=3, node_name="n", auth_token="t", wg_ip="10.77.0.3"))
    assert dbs_docker.publish_address() == "10.77.0.3"
    monkeypatch.setattr(dbs_docker.agent_state, "_load_state", lambda: None)
    monkeypatch.setattr(hub, "_hub", {**hub._hub, "up": False})
    assert dbs_docker.publish_address() is None
    monkeypatch.setattr(hub, "_hub", {**hub._hub, "up": True})
    assert dbs_docker.publish_address() == "10.77.0.1"


def test_the_primary_connects_to_a_remote_database_over_the_node_network(client, monkeypatch):
    from core.database import AsyncSessionLocal
    from core.models import Database, Node
    from dbs import public
    dialed = []

    async def open_connection(host, port):
        dialed.append((host, port))
        return object(), object()
    monkeypatch.setattr(public.asyncio, "open_connection", open_connection)

    async def go():
        async with AsyncSessionLocal() as db:
            node = Node(name="netdb-node", status="online", auth_token="netdb-token", is_local=False, enabled=True, wg_ip="10.77.0.9")
            db.add(node)
            await db.flush()
            database = Database(name="netdb", engine="postgres", version="16", node_id=node.id, status="running", credentials="x")
            db.add(database)
            await db.commit()
            ids = database.id, node.id
        try:
            await public.open_db_stream(ids[0])
        finally:
            async with AsyncSessionLocal() as db:
                await db.delete(await db.get(Database, ids[0]))
                await db.delete(await db.get(Node, ids[1]))
                await db.commit()
        return ids[0]
    db_id = asyncio.run(go())
    assert dialed == [("10.77.0.9", dbs_docker.published_port(db_id))]


def test_the_hub_forwards_between_nodes_once(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parent.parent / "scripts" / "cloudbase-wireguard"
    loader = importlib.machinery.SourceFileLoader("cloudbase_wireguard_net", str(script))
    wg = importlib.util.module_from_spec(importlib.util.spec_from_loader("cloudbase_wireguard_net", loader))
    loader.exec_module(wg)
    calls, present = [], set()

    def run(args, stdin=None):
        calls.append(args)
        if args[:2] == ["iptables", "-C"]:
            if tuple(args[2:]) not in present:
                raise wg.HelperError("no such rule")
        if args[:2] == ["iptables", "-I"]:
            present.add(tuple(args[2:]))
        return ""
    monkeypatch.setattr(wg, "run", run)
    wg.forward_between_nodes()
    wg.forward_between_nodes()
    assert ["sysctl", "-q", "-w", "net.ipv4.ip_forward=1"] in calls
    assert [c for c in calls if c[:2] == ["iptables", "-I"]] == [["iptables", "-I", "FORWARD", "-i", "cbwg0", "-o", "cbwg0", "-j", "ACCEPT"]]


def test_a_node_reports_what_its_databases_are_doing(monkeypatch):
    from agent import report
    sent = []

    async def send(message):
        sent.append(message)
    monkeypatch.setattr(report.dbs_docker, "all_progress", lambda: {4: "Pulling postgres:16 · 40% of 120 MB"})

    async def no_stats():
        return {}
    monkeypatch.setattr(report, "instance_stats", no_stats)
    asyncio.run(report.report_once(1, send))
    assert {"type": "database_progress", "progress": {"4": "Pulling postgres:16 · 40% of 120 MB"}} in sent
