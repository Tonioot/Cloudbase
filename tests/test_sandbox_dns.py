"""Names inside a gVisor sandbox (containers/sandbox_dns.py): gVisor can't reach Docker's own name server
(127.0.0.11), so a sandboxed container gets this machine's real name servers and the addresses of what it is linked to."""
import pytest

SANDBOXED = {"hardening": {"runtime": "runsc"}, "network_access": "internet"}


def test_the_real_name_servers_are_used_not_the_local_stub(tmp_path):
    from containers import sandbox_dns
    stub = tmp_path / "etc-resolv.conf"
    stub.write_text("nameserver 127.0.0.53\noptions edns0\n")
    upstream = tmp_path / "systemd-resolv.conf"
    upstream.write_text("# from systemd\nnameserver 172.26.0.2\nnameserver ::1\nnameserver fe80::1\nsearch ec2.internal\n")
    assert sandbox_dns.name_servers((str(upstream), str(stub))) == ["172.26.0.2"]
    assert sandbox_dns.name_servers((str(tmp_path / "missing"), str(stub))) == list(sandbox_dns.FALLBACK)


def test_the_file_is_written_once_and_kept_while_nothing_changes(tmp_path, monkeypatch):
    from containers import sandbox_dns
    source = tmp_path / "resolv.conf"
    source.write_text("nameserver 10.0.0.2\n")
    monkeypatch.setattr(sandbox_dns, "SOURCES", (str(source),))
    monkeypatch.setattr(sandbox_dns, "RESOLV_FILE", str(tmp_path / "out" / "sandbox-resolv.conf"))
    monkeypatch.setattr(sandbox_dns, "name_servers", lambda sources=sandbox_dns.SOURCES: ["10.0.0.2"])
    path = sandbox_dns.resolv_conf()
    assert open(path).read().endswith("nameserver 10.0.0.2\n")
    before = __import__("os").stat(path).st_ino
    sandbox_dns.resolv_conf()
    assert __import__("os").stat(path).st_ino == before          # not replaced: running containers keep their mount


def test_a_container_that_is_not_sandboxed_is_left_alone():
    from containers import sandbox_dns
    kwargs = {}
    sandbox_dns.apply(None, kwargs, {"hardening": {"runtime": ""}})
    sandbox_dns.apply(None, kwargs, {})
    assert kwargs == {}


def test_a_sandboxed_container_gets_name_servers_and_its_links(monkeypatch):
    from containers import sandbox_dns
    monkeypatch.setattr(sandbox_dns, "resolv_conf", lambda: "/home/cb/.cloudbase/sandbox-resolv.conf")
    monkeypatch.setattr(sandbox_dns, "linked_hosts", lambda docker, options: {"cloudbase-db-7": "172.30.0.2"})
    kwargs = {"volumes": {"data": {"bind": "/data", "mode": "rw"}}}
    sandbox_dns.apply(None, kwargs, SANDBOXED)
    assert kwargs["volumes"]["/home/cb/.cloudbase/sandbox-resolv.conf"] == {"bind": "/etc/resolv.conf", "mode": "ro"}
    assert kwargs["volumes"]["data"] == {"bind": "/data", "mode": "rw"}
    assert kwargs["extra_hosts"] == {"cloudbase-db-7": "172.30.0.2"}


def test_an_app_that_may_not_connect_out_gets_no_name_servers_but_still_its_links(monkeypatch):
    from containers import sandbox_dns
    monkeypatch.setattr(sandbox_dns, "resolv_conf", lambda: pytest.fail("no name servers for it"))
    monkeypatch.setattr(sandbox_dns, "linked_hosts", lambda docker, options: {"app-3": "172.31.0.4"})
    kwargs = {}
    sandbox_dns.apply(None, kwargs, {**SANDBOXED, "network_access": "none"})
    assert "volumes" not in kwargs and kwargs["extra_hosts"] == {"app-3": "172.31.0.4"}


class _Container:
    def __init__(self, network, ip):
        self.attrs = {"NetworkSettings": {"Networks": {network: {"IPAddress": ip}}}}


class _Docker:
    def __init__(self, containers):
        self.containers = self
        self._found = containers

    def list(self, filters=None):
        return self._found.get(filters["name"], [])


def test_the_linked_databases_and_apps_here_are_found_by_their_addresses(monkeypatch):
    from containers import sandbox_dns
    monkeypatch.setattr(sandbox_dns.db_proxy, "local_node_id", lambda: 1)
    monkeypatch.setattr(sandbox_dns.db_proxy, "local_database_ids", lambda linked: [7, 8])

    def address(database_id):
        if database_id == 8:
            raise RuntimeError("not running")
        return "172.30.0.2", 5432
    monkeypatch.setattr(sandbox_dns.dbs_lifecycle, "container_address", address)
    docker = _Docker({"cloudbase-app-3-instance-": [_Container("cloudbase-link-9", "172.31.0.4")]})
    options = {"linked_databases": [{}, {}],
               "linked_apps": [{"alias": "app-3", "network": "cloudbase-link-9", "targets": [{"node_id": 1}]},
                               {"alias": "app-4", "network": "cloudbase-link-10",
                                "targets": [{"node_id": 2, "address": "10.77.0.3", "port": 10004}]}]}
    hosts = sandbox_dns.linked_hosts(docker, options)
    # the database that is not running is left out; app-4 only runs on another node (reached by its address)
    assert hosts == {"cloudbase-db-7": "172.30.0.2", "app-3": "172.31.0.4"}
