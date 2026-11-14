"""Apps cannot reach each other: they all run on one network where containers are cut off from each other, and join
the network of a database only to reach that database."""
import sys
import types

import pytest

from containers import client as cc, naming
from dbs import proxy as db_proxy


class NotFound(Exception):
    pass


class FakeNetwork:
    def __init__(self, docker, name, options):
        self.docker, self.name, self.options = docker, name, options
        self.members: dict[str, "FakeContainer"] = {}
        self.attrs = {}

    def connect(self, container, aliases=None):
        self.members[container.name] = container
        container.networks.add(self.name)
        container.aliases[self.name] = aliases

    def disconnect(self, container):
        self.members.pop(container.name, None)
        container.networks.discard(self.name)

    def reload(self):
        self.attrs = {"Containers": {str(i): {"Name": n} for i, n in enumerate(self.members)}}

    def remove(self):
        del self.docker.network_list[self.name]


class FakeContainer:
    def __init__(self, name, networks=()):
        self.name, self.networks, self.started = name, set(networks), False
        self.attrs, self.aliases = {}, {}

    def reload(self):
        self.attrs = {"NetworkSettings": {"Networks": {n: {} for n in self.networks}}}

    def start(self):
        self.started = True

    def remove(self, force=False):
        pass


class FakeDocker:
    def __init__(self):
        self.network_list: dict[str, FakeNetwork] = {}
        self.container_list: dict[str, FakeContainer] = {}
        self.created = []
        outer = self

        class Networks:
            def get(self, name):
                if name not in outer.network_list:
                    raise NotFound(name)
                return outer.network_list[name]

            def create(self, name, driver, labels, options):
                outer.network_list[name] = FakeNetwork(outer, name, options)

        class Containers:
            def get(self, name):
                if name not in outer.container_list:
                    raise NotFound(name)
                return outer.container_list[name]

            def create(self, img, **kwargs):
                container = FakeContainer(kwargs["name"], [kwargs["network"]])
                outer.network_list[kwargs["network"]].members[container.name] = container
                outer.container_list[container.name] = container
                outer.created.append(kwargs)
                return container

            def run(self, img, **kwargs):
                container = self.create(img, **kwargs)
                container.start()
                return container

        self.networks, self.containers = Networks(), Containers()


@pytest.fixture
def docker(monkeypatch):
    fake = FakeDocker()
    module = types.ModuleType("docker")
    module.errors = types.SimpleNamespace(NotFound=NotFound)
    monkeypatch.setitem(sys.modules, "docker", module)
    monkeypatch.setattr(cc, "_get_client", lambda: fake)
    monkeypatch.setattr(cc, "_firewall_done", True)
    monkeypatch.setattr(db_proxy, "local_node_id", lambda: 1)
    return fake


def link(database_id, node_id=1):
    return {"database_id": database_id, "node_id": node_id, "env_var": f"DB{database_id}_URL",
            "direct_url": f"pg://db{database_id}", "remote_url": f"pg://10.0.0.2:{database_id}"}


def test_apps_run_on_a_network_where_containers_cannot_reach_each_other(docker):
    for blocked, network, bridge in ((True, naming.APPS_NETWORK, "cbapps0"), (False, naming.APPS_LAN_NETWORK, "cbappl0")):
        kwargs = {}
        assert cc._apply_linked_databases(docker, kwargs, {"block_local_network": blocked}) == []
        assert kwargs["network"] == network
        options = docker.network_list[network].options
        assert options["com.docker.network.bridge.enable_icc"] == "false"         # whichever it is: apps do not see each other
        assert options["com.docker.network.bridge.name"] == bridge


def test_an_app_from_before_the_setting_is_not_blocked_and_a_new_one_is(docker):
    old, new = {}, {}
    cc._apply_linked_databases(docker, old, {})                                    # a spec without the key
    cc._apply_linked_databases(docker, new, {"block_local_network": True})
    assert (old["network"], new["network"]) == (naming.APPS_LAN_NETWORK, naming.APPS_NETWORK)


def test_a_cron_job_with_a_database_on_another_node_is_not_blocked(docker):
    options = {"block_local_network": True, "linked_databases": [link(8, node_id=2)]}
    job, instance = {}, {}
    cc._apply_linked_databases(docker, job, options, job=True)                    # the firewall list is made from instances only
    cc._apply_linked_databases(docker, instance, options)
    assert (job["network"], instance["network"]) == (naming.APPS_LAN_NETWORK, naming.APPS_NETWORK)


def test_a_database_network_lets_containers_reach_each_other(docker):
    cc.ensure_database_network(7)
    assert "com.docker.network.bridge.enable_icc" not in docker.network_list["cloudbase-db-7"].options


def test_an_app_joins_the_networks_of_its_databases_on_this_node_only(docker):
    docker.container_list["cloudbase-db-7"] = FakeContainer("cloudbase-db-7")
    kwargs = {}
    networks = cc._apply_linked_databases(docker, kwargs, {"linked_databases": [link(7), link(8, node_id=2)]})
    assert networks == [("cloudbase-db-7", [])]                                  # database 8 runs elsewhere: reached over the node network
    assert kwargs["environment"] == {"DB7_URL": "pg://db7", "DB8_URL": "pg://10.0.0.2:8"}
    assert "cloudbase-db-7" in docker.container_list["cloudbase-db-7"].networks      # the database is on it too


def test_the_link_wins_over_an_env_var_with_the_same_name(docker):
    docker.container_list["cloudbase-db-7"] = FakeContainer("cloudbase-db-7")
    kwargs = {"environment": {"DB7_URL": "pg://from-an-old-env-file", "OTHER": "1"}}
    cc._apply_linked_databases(docker, kwargs, {"linked_databases": [link(7)]})
    assert kwargs["environment"] == {"DB7_URL": "pg://db7", "OTHER": "1"}


def test_the_extra_networks_are_joined_before_the_app_starts(docker):
    docker.container_list["cloudbase-db-7"] = FakeContainer("cloudbase-db-7")
    kwargs = {"name": "app", "detach": True}
    networks = cc._apply_linked_databases(docker, kwargs, {"block_local_network": True, "linked_databases": [link(7)]})
    container = cc.start_container(docker, "img", kwargs, networks)
    assert container.started and container.networks == {naming.APPS_NETWORK, "cloudbase-db-7"}
    assert "detach" not in docker.created[0]


def test_an_old_spec_that_names_the_shared_network_is_ignored(docker):
    kwargs = {}
    cc._apply_linked_databases(docker, kwargs, {"network": "cloudbase", "block_local_network": True})
    assert kwargs["network"] == naming.APPS_NETWORK


def test_the_firewall_is_asked_for_once_and_a_failure_never_stops_an_app(monkeypatch):
    from mesh import helper
    calls = []
    monkeypatch.setattr(cc, "_firewall_done", False)
    monkeypatch.setattr(helper, "isolate_apps", lambda: calls.append(1))
    cc._isolate_from_host()
    cc._isolate_from_host()
    assert calls == [1]

    def old_helper():
        raise helper.HelperError("usage: cloudbase-wireguard")
    monkeypatch.setattr(cc, "_firewall_done", False)
    monkeypatch.setattr(helper, "isolate_apps", old_helper)
    cc._isolate_from_host()                       # only logs: not updated yet means less shut off, not a broken app
    assert cc._firewall_done is False             # and it asks again at the next start


def test_the_old_shared_network_goes_when_only_databases_are_left_on_it(docker):
    legacy = FakeNetwork(docker, naming.LEGACY_NETWORK, {})
    docker.network_list[legacy.name] = legacy
    database = docker.container_list["cloudbase-db-7"] = FakeContainer("cloudbase-db-7", [legacy.name])
    legacy.members["cloudbase-db-7"] = database
    app = docker.container_list["cloudbase-app-1-instance-1"] = FakeContainer("cloudbase-app-1-instance-1", [legacy.name])
    legacy.members[app.name] = app

    cc.retire_legacy_network(docker)
    assert legacy.name in docker.network_list                              # an app that was not restarted yet still uses it

    legacy.disconnect(app)
    cc.retire_legacy_network(docker)
    assert legacy.name not in docker.network_list
    assert database.networks == {"cloudbase-db-7"}                         # the database kept a network of its own


# ── links between apps ────────────────────────────────────────────────────────
def app_link(targets, link_id=5, target=9):
    return {"link_id": link_id, "env_var": "BACKEND_URL", "target_app_id": target, "alias": f"app-{target}", "port": 3000,
            "network": f"cloudbase-link-{link_id}", "targets": targets}


def test_an_app_reaches_a_target_on_its_own_node_by_name_over_a_private_network(docker):
    kwargs = {}
    networks = cc._apply_linked_databases(docker, kwargs, {"linked_apps": [app_link([{"node_id": 1, "port": 20001, "address": "10.0.0.1"}])]})
    assert kwargs["environment"] == {"BACKEND_URL": "http://app-9:3000"}
    assert networks == [("cloudbase-link-5", [])]
    assert docker.network_list["cloudbase-link-5"].options["com.docker.network.bridge.name"] == "cbl5"


def test_a_target_on_another_node_is_reached_at_the_address_its_node_publishes_it_on(docker):
    targets = [{"node_id": 3, "port": 20003, "address": "10.0.0.3"}, {"node_id": 2, "port": 20002, "address": "10.0.0.2"}]
    kwargs = {}
    networks = cc._apply_linked_databases(docker, kwargs, {"linked_apps": [app_link(targets)]})
    assert kwargs["environment"] == {"BACKEND_URL": "http://10.0.0.2:20002"}      # the same one every time: the lowest node
    assert networks == []                                                          # no network shared with another machine


def test_a_target_that_does_not_run_yet_is_reached_by_name_for_when_it_starts_here(docker):
    kwargs = {}
    networks = cc._apply_linked_databases(docker, kwargs, {"linked_apps": [app_link([])]})
    assert kwargs["environment"] == {"BACKEND_URL": "http://app-9:3000"} and networks == [("cloudbase-link-5", [])]


def test_a_target_joins_the_network_of_each_link_to_it_under_its_name(docker):
    exposed = [{"network": "cloudbase-link-5", "alias": "app-9"}, {"network": "cloudbase-link-6", "alias": "app-9"}]
    networks = cc._apply_linked_databases(docker, {}, {"exposed_to": exposed})
    assert networks == [("cloudbase-link-5", ["app-9"]), ("cloudbase-link-6", ["app-9"])]


# ── what blocked apps may still connect to ────────────────────────────────────
def _entry(desired="running", block=True, **options):
    return {"desired": desired, "spec": {"docker_options": {"block_local_network": block, **options}}}


def test_the_firewall_list_has_the_other_nodes_targets_of_blocked_apps_only(monkeypatch):
    from containers import egress
    monkeypatch.setattr(db_proxy, "local_node_id", lambda: 1)
    remote_app = app_link([{"node_id": 2, "port": 20002, "address": "10.0.0.2"}])
    same_node = app_link([{"node_id": 1, "port": 20001, "address": "10.0.0.1"}], link_id=6)
    database = link(8, node_id=2)
    database["remote_url"] = "postgresql://user:p%40ss@10.0.0.2:30008/app"
    entries = {
        1: _entry(linked_apps=[remote_app, same_node], linked_databases=[database, link(7, node_id=1)]),
        2: _entry(block=False, linked_apps=[app_link([{"node_id": 3, "port": 20003, "address": "10.0.0.3"}], link_id=7)]),   # may reach it anyway
        3: _entry(desired="stopped", linked_apps=[app_link([{"node_id": 4, "port": 20004, "address": "10.0.0.4"}], link_id=8)]),
    }
    assert egress.allowed_targets(entries) == [{"ip": "10.0.0.2", "port": 20002}, {"ip": "10.0.0.2", "port": 30008}]


def test_a_database_address_is_read_from_its_url():
    from containers import egress
    assert egress.endpoint("postgresql://u:pw@10.77.0.3:30007/db") == ("10.77.0.3", 30007)
    assert egress.endpoint("redis://default:a@b@10.77.0.3:30009/0") == ("10.77.0.3", 30009)       # an @ in the password
    assert egress.endpoint("mongodb://u:p@10.77.0.3:30010/db?authSource=admin") == ("10.77.0.3", 30010)
    assert egress.endpoint("not a url") is None


def test_the_helper_is_only_called_when_the_list_changes(monkeypatch):
    from containers import egress
    from mesh import helper
    sent = []
    monkeypatch.setattr(db_proxy, "local_node_id", lambda: 1)
    monkeypatch.setattr(helper, "set_egress", lambda allowed: sent.append(allowed))
    monkeypatch.setattr(egress, "_applied", None)
    entries = {1: _entry(linked_apps=[app_link([{"node_id": 2, "port": 20002, "address": "10.0.0.2"}])])}
    egress.apply(entries)
    egress.apply(entries)
    assert sent == [[{"ip": "10.0.0.2", "port": 20002}]]
    egress.apply({})
    assert sent[-1] == []                                   # a connection that is gone is closed again
