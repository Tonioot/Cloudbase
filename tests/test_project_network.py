"""Everything in a connected project reaches its apps and databases at addresses that never change: what an app gets
(apps/app_links.py), the rules a machine makes of where things run (containers/routes.py) and what the root helper
accepts (scripts/cloudbase-wireguard)."""
import asyncio
import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

from apps import app_links
from containers import client as containers_client, naming, routes
from core import projects as core_projects
from core.database import AsyncSessionLocal
from core.env_crypto import encrypt_env, encrypt_text
from core.models import Application, Database, Instance, Node, Project

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "cloudbase-wireguard"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def shop(client):
    """A connected project with a web app on a node, a worker and two databases, and an app in another project."""
    ids = {}

    async def make():
        async with AsyncSessionLocal() as db:
            mine, other = Project(name="pn-shop", apps_connected=True), Project(name="pn-other", apps_connected=True)
            node = Node(name="pn-node", status="online", auth_token="pn-token", is_local=False, enabled=True, wg_ip="10.77.0.9")
            db.add_all([mine, other, node])
            await db.flush()
            api = Application(name="pn-api", repo_url="https://example.com/a.git", port=3000, project_id=mine.id)
            worker = Application(name="pn-worker", repo_url="https://example.com/w.git", no_web=True, project_id=mine.id,
                                 env_vars=encrypt_env({"OWN_URL": "mine"}))
            stranger = Application(name="pn-stranger", repo_url="https://example.com/s.git", port=80, project_id=other.id)
            login = encrypt_text('{"user": "u", "password": "p", "db_name": "main"}')
            first = Database(name="pn-db", project_id=mine.id, engine="postgres", version="17", node_id=node.id,
                             credentials=login, node_port=30011, env_var_name="SHOP_DB_URL")
            second = Database(name="own", project_id=mine.id, engine="postgres", version="17", node_id=node.id,
                              credentials=login, node_port=30012)
            db.add_all([api, worker, stranger, first, second])
            await db.flush()
            live = Instance(app_id=api.id, node_id=node.id, external_port=20042, status="running")
            coming = Instance(app_id=api.id, node_id=node.id, external_port=20043, status="starting")
            db.add_all([live, coming])
            await db.commit()
            ids.update(mine=mine.id, other=other.id, node=node.id, api=api.id, worker=worker.id, stranger=stranger.id,
                       first=first.id, second=second.id, live=live.id, coming=coming.id)
        await core_projects.refresh()

    async def clean():
        async with AsyncSessionLocal() as db:
            for model, keys in ((Instance, ("live", "coming")), (Database, ("first", "second")),
                                (Application, ("api", "worker", "stranger")), (Node, ("node",)), (Project, ("mine", "other"))):
                for key in keys:
                    row = await db.get(model, ids[key])
                    if row is not None:
                        await db.delete(row)
                await db.commit()
        await core_projects.refresh()
    run(make())
    yield ids
    run(clean())


def _options(app_id):
    async def go():
        async with AsyncSessionLocal() as db:
            return await app_links.runtime_options(db, await db.get(Application, app_id))
    return run(go())


def test_an_app_gets_every_database_and_web_app_of_its_project_at_a_fixed_address(shop):
    got = {e["env_var"]: e for e in _options(shop["worker"])["project"]}
    # the database named by hand, and the web app; "own" would be OWN_URL, which the worker sets itself
    assert sorted(got) == ["PN_API_URL", "SHOP_DB_URL"]
    api, database = naming.route_port(app_id=shop["api"]), naming.route_port(database_id=shop["first"])
    assert got["PN_API_URL"]["url"] == f"http://app-{shop['api']}:{api}"
    assert got["SHOP_DB_URL"]["url"] == f"postgresql://u:p@db-{shop['first']}:{database}/main"
    assert got["SHOP_DB_URL"]["network"] == f"cloudbase-project-{shop['mine']}"
    assert [e["env_var"] for e in _options(shop["api"])["project"]] == ["SHOP_DB_URL", "OWN_URL"]   # not itself
    assert "project" not in _options(shop["stranger"])                      # nothing of another project


def test_a_project_that_is_not_connected_gives_nothing(shop):
    async def off():
        async with AsyncSessionLocal() as db:
            (await db.get(Project, shop["mine"])).apps_connected = False
            await db.commit()
        await core_projects.refresh()
    run(off())
    assert "project" not in _options(shop["worker"]) and "exposed_to" not in _options(shop["api"])


def test_a_cron_run_gets_the_addresses_here_and_on_a_node(shop, monkeypatch):
    from cron import scheduler
    seen = {}

    async def image(app, push):
        return "img"

    def job(app_id, run_id, img, command, env, options, timeout, on_line=None):
        seen["here"] = options
        return {"exit_code": 0}

    class Done:
        status, result, error_message = "done", {"exit_code": 0}, None

    async def ask(node_id, command, payload, **kwargs):
        seen["node"] = payload["docker_options"]
        return Done()
    monkeypatch.setattr(scheduler, "_ensure_local_image", image)
    monkeypatch.setattr(scheduler.containers_jobs, "run_job_container", job)
    monkeypatch.setattr(scheduler.node_commands, "ask", ask)
    monkeypatch.setattr(scheduler.apps_runtime, "_refresh_app_source_revision", lambda app: "abc")

    async def go():
        async with AsyncSessionLocal() as db:
            app, node = await db.get(Application, shop["worker"]), await db.get(Node, shop["node"])
        await scheduler._run_local(app, 1, lambda line: None)
        await scheduler._run_remote(app, node, 2)
    run(go())
    for where in ("here", "node"):
        assert [e["env_var"] for e in seen[where]["project"]] == ["SHOP_DB_URL", "PN_API_URL"]


def test_a_node_may_fetch_the_source_of_the_cron_job_it_runs_and_only_then(shop):
    from fastapi import HTTPException

    from core.models import CronRun
    from routers.applications.deps import _require_node_agent_for_app

    class Request:
        headers = {"X-Node-Token": "pn-token"}

    async def fetch(status):
        async with AsyncSessionLocal() as db:
            row = CronRun(app_id=shop["worker"], node_id=shop["node"], status=status)
            db.add(row)
            await db.commit()
            try:
                await _require_node_agent_for_app()(Request(), shop["worker"], db)
                return True
            except HTTPException as e:
                assert e.status_code == 403
                return False
            finally:
                await db.delete(row)
                await db.commit()
    assert run(fetch("success")) is False and run(fetch("running")) is True


def test_two_databases_never_share_a_variable():
    rows = [Database(id=2, name="shop_db"), Database(id=1, name="shop-db"), Database(id=3, name="x", env_var_name="SHOP_DB_URL")]
    assert [name for name, _ in app_links.database_variables(rows)] == ["SHOP_DB_URL", "SHOP_DB_2_URL", "SHOP_DB_3_URL"]


def test_the_routes_say_where_healthy_instances_and_databases_are(shop):
    async def go():
        async with AsyncSessionLocal() as db:
            return await app_links.project_routes(db)
    found = {r["port"]: r for r in run(go()) if r["network"] == f"cloudbase-project-{shop['mine']}"}
    api = found[naming.route_port(app_id=shop["api"])]
    assert api["targets"] == [{"node_id": shop["node"], "container": f"cloudbase-app-{shop['api']}-instance-{shop['live']}",
                               "internal_port": 3000, "port": 20042, "address": "10.77.0.9"}]      # not the starting one
    database = found[naming.route_port(database_id=shop["first"])]["targets"][0]
    assert (database["container"], database["internal_port"], database["port"], database["address"]) == (
        f"cloudbase-db-{shop['first']}", 5432, 30011, "10.77.0.9")
    assert naming.route_port(app_id=shop["worker"]) not in found                 # a worker has no web server


def test_the_connections_of_an_app_list_what_the_project_gives_it(admin, shop):
    listed = admin.get(f"/api/apps/{shop['worker']}/connections").json()["project"]
    assert [(e["kind"], e["env_var_name"]) for e in listed] == [("database", "SHOP_DB_URL"), ("app", "PN_API_URL")]
    assert "password" not in str(listed) and "://" not in str(listed)


def test_the_variable_of_a_database_can_be_given_and_taken_back(admin, shop):
    path = f"/api/databases/{shop['first']}"
    assert admin.patch(path, json={"env_var_name": "bad name"}).status_code == 400
    assert admin.patch(path, json={"env_var_name": "other_url"}).json() == {"ok": True, "changed": True}
    assert admin.get(path).json()["env_var_name"] == "OTHER_URL"
    assert admin.patch(path, json={"env_var_name": ""}).status_code == 200
    assert admin.get(path).json()["env_var_name"] is None


# ── on the machine ────────────────────────────────────────────────────────────
class NotFound(Exception):
    pass


class _Found:
    def __init__(self, **attrs):
        self.__dict__.update(attrs)


class _Docker:
    def __init__(self, networks, containers):
        self.networks, self.containers = _Table(networks), _Table(containers)


class _Table:
    def __init__(self, rows):
        self.rows = rows

    def get(self, name):
        if name not in self.rows:
            import docker
            raise docker.errors.NotFound(name)
        return self.rows[name]


def _network(bridge, gateway):
    return _Found(attrs={"Options": {"com.docker.network.bridge.name": bridge}, "IPAM": {"Config": [{"Gateway": gateway}]}})


def _container(addresses, status="running", port=None):
    return _Found(status=status, labels={"cloudbase.internal_port": str(port)} if port else {},
                  attrs={"NetworkSettings": {"Networks": {n: {"IPAddress": ip} for n, ip in addresses.items()}}})


def test_a_target_here_is_reached_directly_and_one_elsewhere_at_its_node():
    pytest.importorskip("docker")
    net = "cloudbase-project-5"
    docker = _Docker({net: _network("cblp5", "172.20.0.1")},
                     {"here": _container({"cloudbase-apps": "172.18.0.4", net: "172.20.0.3"}, port=8080),
                      "db": _container({"cloudbase-db-1": "172.21.0.2"}),
                      "gone": _container({net: "172.20.0.9"}, status="exited")})
    wanted = [
        {"network": net, "port": 20001, "targets": [
            {"node_id": 1, "container": "here", "internal_port": 3000},
            {"node_id": 1, "container": "gone", "internal_port": 3000},
            {"node_id": 1, "container": "missing", "internal_port": 3000},
            {"node_id": 2, "container": "far", "internal_port": 3000, "address": "10.77.0.3", "port": 10004},
            {"node_id": 3, "container": "unpublished", "internal_port": 3000, "address": None, "port": 10005}]},
        {"network": net, "port": 40001, "targets": [{"node_id": 1, "container": "db", "internal_port": 5432}]},
        {"network": net, "port": 20002, "targets": [{"node_id": 1, "container": "gone", "internal_port": 3000}]},
        {"network": "cloudbase-project-6", "port": 20003, "targets": [{"node_id": 2, "address": "10.77.0.3", "port": 1}]},
    ]
    assert routes.rules(docker, wanted, me=1) == [
        {"bridge": "cblp5", "gateway": "172.20.0.1", "port": 20001,
         "targets": [{"ip": "172.20.0.3", "port": 8080}, {"ip": "10.77.0.3", "port": 10004}]},
        {"bridge": "cblp5", "gateway": "172.20.0.1", "port": 40001, "targets": [{"ip": "172.21.0.2", "port": 5432}]}]


def test_nothing_changes_before_the_primary_said_where_things_are(monkeypatch):
    monkeypatch.setattr(routes, "_wanted", None)
    monkeypatch.setattr(routes.helper, "set_routes", lambda rules: pytest.fail("no list yet"))
    routes.apply()


def test_the_rules_are_sent_when_they_change(monkeypatch):
    sent, now = [], [[]]
    monkeypatch.setattr(routes, "_wanted", None)
    monkeypatch.setattr(routes, "_applied", None)
    monkeypatch.setattr(routes, "_failed", None)
    monkeypatch.setattr(routes.containers_client, "_get_client", lambda: None)
    monkeypatch.setattr(routes, "rules", lambda docker, wanted, me: now[0])
    monkeypatch.setattr(routes.helper, "set_routes", sent.append)
    routes.set_wanted([], 1)
    routes.apply()
    routes.apply()
    now[0] = [{"port": 1}]
    routes.apply()
    assert sent == [[], [{"port": 1}]]


def test_a_start_gets_the_variables_and_the_names_on_the_project_network(monkeypatch):
    monkeypatch.setattr(containers_client, "ensure_link_network", lambda name: name)
    monkeypatch.setattr(containers_client, "network_gateway", lambda client, name: "172.20.0.1")
    kwargs = {"extra_hosts": {"other": "1.2.3.4"}}
    env, joined = containers_client._project_addresses(None, {"project": [
        {"env_var": "API_URL", "url": "http://app-2:20002", "alias": "app-2", "network": "cloudbase-project-5"},
        {"env_var": "DB_URL", "url": "postgresql://u:p@db-1:40001/main", "alias": "db-1", "network": "cloudbase-project-5"}]},
        kwargs)
    assert env == {"API_URL": "http://app-2:20002", "DB_URL": "postgresql://u:p@db-1:40001/main"}
    assert kwargs["extra_hosts"] == {"other": "1.2.3.4", "app-2": "172.20.0.1", "db-1": "172.20.0.1"}
    assert containers_client._merged(joined + [("cloudbase-project-5", ["app-3"])]) == [("cloudbase-project-5", ["app-3"])]


def test_project_and_link_networks_get_bridges_of_their_own(monkeypatch):
    made = []
    monkeypatch.setattr(containers_client, "ensure_network", lambda name, bridge: made.append(bridge))
    containers_client.ensure_link_network("cloudbase-project-2")
    containers_client.ensure_link_network("cloudbase-link-2")
    assert made == ["cblp2", "cbll2"]


# ── the root helper ───────────────────────────────────────────────────────────
@pytest.fixture
def wg(tmp_path, monkeypatch):
    loader = importlib.machinery.SourceFileLoader("cloudbase_wireguard", str(SCRIPT))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("cloudbase_wireguard", loader))
    loader.exec_module(module)
    monkeypatch.setattr(module, "CONF_DIR", str(tmp_path))
    return module


ROUTE = {"bridge": "cblp5", "gateway": "172.20.0.1", "port": 20001,
         "targets": [{"ip": "172.20.0.3", "port": 80}, {"ip": "10.77.0.3", "port": 10004},
                     {"ip": "10.77.0.4", "port": 10001}]}


def test_the_helper_writes_rules_for_the_projects_own_network_only(wg):
    text = wg.routes_rules(wg.check_routes([ROUTE]))
    forwards = [line for line in text.splitlines() if "--to-destination" in line]
    assert len(forwards) == 3 and all("-i cblp5 -d 172.20.0.1/32" in line and "--dport 20001" in line for line in forwards)
    assert "--probability 0.3333333333" in forwards[0] and "--probability 0.5000000000" in forwards[1]
    assert "statistic" not in forwards[2]
    assert "-A CLOUDBASE-ROUTES-FWD -m conntrack --ctstate DNAT --ctorigdst 172.20.0.1 -j ACCEPT" in text
    assert "-A CLOUDBASE-ROUTES-SNAT -m conntrack --ctstate DNAT --ctorigdst 172.20.0.1 -j MASQUERADE" in text
    empty = wg.routes_rules([])
    assert ":CLOUDBASE-ROUTES - [0:0]" in empty and "-A " not in empty         # the chains are emptied


@pytest.mark.parametrize("change", [
    {"bridge": "eth0"}, {"bridge": "cbapps0"}, {"bridge": "cblp5 -j ACCEPT"}, {"bridge": "cblp5\n-A INPUT -j ACCEPT"},
    {"gateway": "8.8.8.8"}, {"gateway": "127.0.0.1"}, {"gateway": "172.20.0.1; id"}, {"port": 0}, {"port": "80"},
    {"port": True}, {"targets": []}, {"targets": [{"ip": "169.254.169.254", "port": 80}]},
    {"targets": [{"ip": "127.0.0.1", "port": 80}]}, {"targets": [{"ip": "1.1.1.1", "port": 80}]},
    {"targets": [{"ip": "10.0.0.1", "port": 70000}]}, {"targets": ["10.0.0.1"]},
])
def test_the_helper_refuses_anything_else(wg, change):
    with pytest.raises(wg.HelperError):
        wg.check_routes([{**ROUTE, **change}])


def test_one_address_has_one_route(wg):
    with pytest.raises(wg.HelperError):
        wg.check_routes([ROUTE, ROUTE])


def test_the_routes_are_kept_for_the_next_boot(wg, monkeypatch):
    import json
    ran = []
    monkeypatch.setattr(wg, "run", lambda args, stdin=None: ran.append(args) or _fail_delete(wg, args))
    assert json.loads(wg.cmd_routes([], json.dumps({"routes": [ROUTE]}))) == {"routes": 1}
    assert wg.load_routes() == [ROUTE]
    assert ["iptables-restore", "-w", "5", "--noflush"] in ran
    assert ["iptables", "-w", "5", "-t", "filter", "-I", "DOCKER-USER", "-j", "CLOUDBASE-ROUTES-FWD"] in ran


def test_the_bypass_for_the_bridges_stays_below_the_project_addresses(wg, monkeypatch):
    ran = []
    monkeypatch.setattr(wg, "run", lambda args, stdin=None: ran.append(args) or _fail_delete(wg, args))
    wg.isolate_apps()
    assert not any("CLOUDBASE-ROUTES" in args for args in ran)               # none set here yet
    wg.cmd_routes([], '{"routes": []}')
    ran.clear()
    wg.isolate_apps()
    jump = ["iptables", "-w", "5", "-t", "nat", "-I", "PREROUTING", "-j", "CLOUDBASE-ROUTES"]
    assert ran.index(jump) > max(i for i, args in enumerate(ran) if "addrtype" in args)


def _fail_delete(wg, args):
    if "-D" in args or "-C" in args and "addrtype" in args:
        raise wg.HelperError("no such rule")
    return ""
