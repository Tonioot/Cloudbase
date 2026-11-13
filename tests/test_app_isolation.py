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

    def connect(self, container):
        self.members[container.name] = container
        container.networks.add(self.name)

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
        self.attrs = {}

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
    kwargs = {}
    assert cc._apply_linked_databases(docker, kwargs, {}) == []
    assert kwargs["network"] == naming.APPS_NETWORK
    assert docker.network_list[naming.APPS_NETWORK].options["com.docker.network.bridge.enable_icc"] == "false"


def test_a_database_network_lets_containers_reach_each_other(docker):
    cc.ensure_database_network(7)
    assert "com.docker.network.bridge.enable_icc" not in docker.network_list["cloudbase-db-7"].options


def test_an_app_joins_the_networks_of_its_databases_on_this_node_only(docker):
    docker.container_list["cloudbase-db-7"] = FakeContainer("cloudbase-db-7")
    kwargs = {}
    networks = cc._apply_linked_databases(docker, kwargs, {"linked_databases": [link(7), link(8, node_id=2)]})
    assert networks == ["cloudbase-db-7"]                                  # database 8 runs elsewhere: reached over the node network
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
    networks = cc._apply_linked_databases(docker, kwargs, {"linked_databases": [link(7)]})
    container = cc.start_container(docker, "img", kwargs, networks)
    assert container.started and container.networks == {naming.APPS_NETWORK, "cloudbase-db-7"}
    assert "detach" not in docker.created[0]


def test_an_old_spec_that_names_the_shared_network_is_ignored(docker):
    kwargs = {}
    cc._apply_linked_databases(docker, kwargs, {"network": "cloudbase"})
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
