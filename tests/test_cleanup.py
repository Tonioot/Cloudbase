from types import SimpleNamespace

import pytest

from containers import manager as dm

GB = 1024 ** 3


class _FakeImages:
    def __init__(self, fail=False):
        self.filters = None
        self.fail = fail

    def prune(self, filters=None):
        self.filters = filters
        if self.fail:
            raise RuntimeError("daemon unreachable")
        return {"ImagesDeleted": [{"Untagged": "a"}, {"Deleted": "b"}], "SpaceReclaimed": 2 * GB}


class _FakeApi:
    def __init__(self):
        self.keep = "unset"

    def prune_builds(self, keep_storage=None):
        self.keep = keep_storage
        return {"SpaceReclaimed": 3 * GB}


@pytest.fixture
def client(monkeypatch):
    c = SimpleNamespace(images=_FakeImages(), api=_FakeApi())
    monkeypatch.setattr(dm, "_docker_client", c)
    monkeypatch.setattr(dm, "disk_percent", lambda: 50.0)
    monkeypatch.setitem(dm._build_locks, 99, dm.threading.Lock())  # isolate from other tests
    yield c
    dm._build_locks.pop(99, None)


def test_routine_cleanup_keeps_recent_images_and_5gb_of_cache(client):
    res = dm.cleanup_docker()
    assert client.images.filters == {"dangling": True, "until": "24h"}
    assert client.api.keep == dm.BUILD_CACHE_KEEP_BYTES == 5 * GB
    assert res["images_removed"] == 2 and res["images_bytes"] == 2 * GB and res["cache_bytes"] == 3 * GB
    assert "Removed 2" in res["summary"] and "2.0 GB" in res["summary"] and "3.0 GB" in res["summary"]


def test_aggressive_cleanup_removes_all_untagged_and_trims_cache_harder(client):
    dm.cleanup_docker(aggressive=True)
    assert client.images.filters == {"dangling": True}
    assert client.api.keep == dm.BUILD_CACHE_KEEP_BYTES_LOW_DISK == 1 * GB


def test_only_untagged_images_are_ever_pruned(client):
    """Tagged images (apps' current images, even stopped apps) must survive."""
    for aggressive in (False, True):
        dm.cleanup_docker(aggressive=aggressive)
        assert client.images.filters["dangling"] is True


def test_cleanup_is_skipped_while_another_app_builds(client):
    lock = dm._get_build_lock(7)
    lock.acquire()
    try:
        assert dm.cleanup_docker() == {"skipped": "A build is in progress"}
        assert client.images.filters is None  # nothing was pruned
        # ...but the app that is about to build may clean up for itself
        assert "summary" in dm.cleanup_docker(own_app_id=7)
    finally:
        lock.release()
        dm._build_locks.pop(7, None)


def test_a_second_cleanup_cannot_run_at_the_same_time(client):
    assert dm._cleanup_lock.acquire(blocking=False)
    try:
        assert dm.cleanup_docker() == {"skipped": "A cleanup is already running"}
    finally:
        dm._cleanup_lock.release()


def test_failure_in_one_step_does_not_stop_the_other(client):
    client.images.fail = True
    res = dm.cleanup_docker()
    assert res["errors"] and "images" in res["errors"][0]
    assert res["cache_bytes"] == 3 * GB


def test_old_docker_py_without_keep_storage_still_works(client, monkeypatch):
    def old(**kw):
        if kw:
            raise TypeError("unexpected keyword")
        return {"SpaceReclaimed": GB}
    monkeypatch.setattr(client.api, "prune_builds", old)
    assert dm.cleanup_docker()["cache_bytes"] == GB


def test_pre_build_cleanup_only_when_the_disk_is_nearly_full(client, monkeypatch):
    lines = []
    push = lambda app_id, line: lines.append(line)

    monkeypatch.setattr(dm, "disk_percent", lambda: 60.0)
    dm._cleanup_before_build(1, push)
    assert lines == [] and client.images.filters is None

    monkeypatch.setattr(dm, "disk_percent", lambda: 90.0)
    dm._cleanup_before_build(1, push)
    assert client.images.filters == {"dangling": True}  # aggressive
    assert any("90% full" in l for l in lines) and any("Removed" in l for l in lines)


def test_pre_build_cleanup_never_fails_the_build(monkeypatch):
    monkeypatch.setattr(dm, "disk_percent", lambda: 99.0)
    monkeypatch.setattr(dm, "cleanup_docker", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    dm._cleanup_before_build(1, lambda *a: None)  # must not raise


def test_byte_formatting():
    assert dm._fmt_bytes(0) == "0 B"
    assert dm._fmt_bytes(1536) == "1.5 KB"
    assert dm._fmt_bytes(5 * GB) == "5.0 GB"
