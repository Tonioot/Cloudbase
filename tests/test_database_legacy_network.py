"""A database container made when all containers shared one network is still set to start on it. That network is
removed once nothing needs it, and then the container can no longer be started or restarted."""
from types import SimpleNamespace

from dbs import lifecycle


def _container(network_mode):
    removed = []
    return SimpleNamespace(attrs={"HostConfig": {"NetworkMode": network_mode}}, id="c1", remove=lambda **kw: removed.append(kw),
                           restart=lambda **kw: (_ for _ in ()).throw(AssertionError("restarted a container that cannot start")),
                           removed=removed)


def test_a_container_set_to_the_old_shared_network_is_recognised():
    assert lifecycle._on_removed_network(_container("cloudbase"))
    assert not lifecycle._on_removed_network(_container("cloudbase-db-7"))
    assert not lifecycle._on_removed_network(SimpleNamespace(attrs={}))


def test_restart_replaces_such_a_container_instead_of_restarting_it(monkeypatch):
    old = _container("cloudbase")
    monkeypatch.setattr(lifecycle, "_client", lambda: object())
    monkeypatch.setattr(lifecycle, "_get_container", lambda client, db_id: old)
    monkeypatch.setattr(lifecycle, "_publishes_elsewhere", lambda c, db_id: False)
    monkeypatch.setattr(lifecycle, "ensure_running", lambda spec: {"state": "running", "replaced": True})
    assert lifecycle.restart({"database_id": 7}) == {"state": "running", "replaced": True}


def test_ensure_running_removes_such_a_container_and_makes_a_new_one(monkeypatch):
    old = _container("cloudbase")
    fresh = SimpleNamespace(id="c2")
    monkeypatch.setattr(lifecycle, "_client", lambda: SimpleNamespace(volumes=SimpleNamespace(get=lambda n: object())))
    monkeypatch.setattr(lifecycle, "_get_container", lambda client, db_id: old)
    monkeypatch.setattr(lifecycle, "_publishes_elsewhere", lambda c, db_id: False)
    monkeypatch.setattr(lifecycle, "_volume_exists", lambda client, db_id: True)
    monkeypatch.setattr(lifecycle, "_run_container", lambda client, spec: fresh)
    monkeypatch.setattr(lifecycle, "_container_state", lambda c: "running")
    r = lifecycle.ensure_running({"database_id": 7, "initialized": True, "engine": "postgres", "version": "16"})
    assert old.removed and r["container_id"] == "c2"
