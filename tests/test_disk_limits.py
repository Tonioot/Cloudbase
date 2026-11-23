"""Disk limits of databases: the root helper (scripts/cloudbase-quota), its client, and what the database code does
with a limit (a disk of its own, made once, only growing, removed with the data)."""
import asyncio
import importlib.machinery
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "cloudbase-quota"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def helper(tmp_path, monkeypatch):
    loader = importlib.machinery.SourceFileLoader("cloudbase_quota", str(SCRIPT))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("cloudbase_quota", loader))
    loader.exec_module(module)
    monkeypatch.setattr(module, "DISK_DIR", str(tmp_path / "disks"))
    return module


# ── the helper ────────────────────────────────────────────────────────────────
def test_the_helper_only_takes_numbers_in_range(helper):
    assert helper.check_id("7") == 7 and helper.check_mb("1024") == 1024
    for bad in ("0", "-1", "7; rm -rf /", "../7", "1e3", "", None, "1000000000"):
        with pytest.raises(helper.HelperError):
            helper.check_id(bad)
    for bad in ("10", "abc", "99999999999", "512M"):
        with pytest.raises(helper.HelperError):
            helper.check_mb(bad)


class _FakeMachine:
    """mount/fallocate/... as the helper calls them: files are made, mounts are remembered."""

    def __init__(self, helper, monkeypatch):
        self.calls, self.mounted = [], set()
        self.helper = helper
        monkeypatch.setattr(helper, "run", self.run)
        monkeypatch.setattr(helper, "is_mounted", lambda path: path in self.mounted)
        monkeypatch.setattr(helper, "_info", lambda database_id: {"size_mb": helper._size_mb(database_id)})

    def run(self, args, timeout=120):
        self.calls.append(args)
        if args[0] == "fallocate":
            with open(args[-1], "ab") as f:
                f.truncate(int(args[2][:-1]) * 1024 * 1024)
        elif args[0] == "mount":
            self.mounted.add(args[-1])
        elif args[0] == "umount":
            self.mounted.discard(args[-1])
        elif args[0] == "losetup" and args[1] == "-j":
            return "/dev/loop3: []: (" + args[2] + ")\n"
        return ""

    def programs(self):
        return [c[0] for c in self.calls]


def test_a_new_disk_is_reserved_formatted_and_mounted_with_a_data_directory(helper, monkeypatch):
    machine = _FakeMachine(helper, monkeypatch)
    info = helper.create(7, 256)
    assert info["size_mb"] == 256
    assert machine.programs() == ["fallocate", "mkfs.ext4", "mount"]
    assert machine.calls[2][:3] == ["mount", "-o", "loop,nodev,nosuid,noatime"]
    assert os.path.isdir(os.path.join(helper.mount_point(7), "data"))         # the volume binds here
    assert not os.path.exists(helper.image_path(7) + ".new")

    machine.calls.clear()
    helper.create(7, 256)                                 # again (a retried start): nothing is formatted twice
    assert "mkfs.ext4" not in machine.programs() and "fallocate" not in machine.programs()


def test_a_disk_grows_online_and_never_shrinks(helper, monkeypatch):
    machine = _FakeMachine(helper, monkeypatch)
    helper.create(7, 256)
    machine.calls.clear()
    assert helper.grow(7, 1024)["size_mb"] == 1024
    assert machine.programs() == ["fallocate", "losetup", "losetup", "resize2fs"]
    assert machine.calls[2] == ["losetup", "-c", "/dev/loop3"] and machine.calls[3] == ["resize2fs", "/dev/loop3"]
    with pytest.raises(helper.HelperError, match="only grow"):
        helper.grow(7, 512)
    with pytest.raises(helper.HelperError, match="no disk"):
        helper.grow(8, 512)


def test_removing_a_disk_unmounts_it_and_deletes_the_image(helper, monkeypatch):
    machine = _FakeMachine(helper, monkeypatch)
    helper.create(7, 256)
    os.rmdir(os.path.join(helper.mount_point(7), "data"))          # it lives on the disk, gone once unmounted
    helper.remove(7)
    assert "umount" in machine.programs()
    assert not os.path.exists(helper.image_path(7)) and not os.path.exists(helper.mount_point(7))


def test_boot_mounts_every_disk_there_is(helper, monkeypatch):
    machine = _FakeMachine(helper, monkeypatch)
    helper.create(3, 256)
    helper.create(12, 256)
    machine.mounted.clear()
    assert helper.boot() == {"mounted": [12, 3], "failed": []}
    assert len(machine.mounted) == 2


def test_the_helper_refuses_to_run_without_root(helper, monkeypatch, capsys):
    monkeypatch.delenv("CLOUDBASE_QUOTA_ALLOW_NON_ROOT_FOR_TESTS_ONLY", raising=False)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("running as root")
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    assert helper.main(["boot"]) == 1
    assert "root" in capsys.readouterr().err


# ── the client ────────────────────────────────────────────────────────────────
@pytest.fixture
def fake_helper(tmp_path, monkeypatch):
    script = tmp_path / "fake.py"
    script.write_text(
        "import sys, json\n"
        "if sys.argv[1] == 'fail':\n    print('error: boom', file=sys.stderr); sys.exit(1)\n"
        "if sys.argv[1] == 'traffic':\n    ips = json.load(sys.stdin)['ips']\n"
        "    print(json.dumps({'bytes': {ip: 100 for ip in ips}}))\n"
        "else:\n    print(json.dumps({'args': sys.argv[1:]}))\n")
    monkeypatch.setenv("CLOUDBASE_QUOTA_HELPER", f"{sys.executable} {script}")


def test_the_client_talks_to_the_helper(fake_helper):
    from containers import quota
    assert quota.create_disk(7, 512) == {"args": ["disk-create", "7", "512"]}
    assert quota.grow_disk(7, 1024) == {"args": ["disk-grow", "7", "1024"]}
    assert quota.traffic(["172.20.0.5", "172.20.0.5"]) == {"172.20.0.5": 100}
    with pytest.raises(quota.QuotaError, match="boom"):
        quota._run(["fail"])


def test_the_client_says_how_to_install_a_missing_helper(monkeypatch):
    from containers import quota
    monkeypatch.setenv("CLOUDBASE_QUOTA_HELPER", "/definitely/not/here")
    with pytest.raises(quota.QuotaError, match="cloudbase quota"):
        quota.mount_disk(7)


# ── the database code ─────────────────────────────────────────────────────────
def test_a_limit_must_leave_the_engine_room_and_can_only_grow():
    from fastapi import HTTPException
    from dbs.provision import validate_disk_limit
    validate_disk_limit("postgres", None)
    validate_disk_limit("postgres", 256)
    validate_disk_limit("mysql", 2048, current_mb=1024)
    for engine, mb, current in (("postgres", 100, None), ("mysql", 512, None), ("postgres", 512, 1024)):
        with pytest.raises(HTTPException):
            validate_disk_limit(engine, mb, current_mb=current)


class _FakeQuota:
    def __init__(self, monkeypatch, disk=False, mounted=True, size=1024):
        from dbs import lifecycle
        self.disk, self.mounted, self.size, self.calls = disk, mounted, size, []
        q = lifecycle.quota
        for name, fn in {"has_disk": lambda i: self.disk, "is_mounted": lambda i: self.mounted,
                         "image_size_mb": lambda i: self.size,
                         "mount_disk": lambda i: self.calls.append(("mount", i)),
                         "grow_disk": lambda i, mb: self.calls.append(("grow", i, mb)),
                         "create_disk": lambda i, mb: self.calls.append(("create", i, mb))}.items():
            monkeypatch.setattr(q, name, fn)


def test_a_database_without_a_limit_gets_no_disk(monkeypatch):
    from dbs import lifecycle
    fake = _FakeQuota(monkeypatch)
    assert lifecycle._ensure_disk({"database_id": 7}) is False and fake.calls == []


def test_a_new_database_gets_a_disk_of_its_limit(monkeypatch):
    from dbs import lifecycle
    fake = _FakeQuota(monkeypatch, disk=False)
    assert lifecycle._ensure_disk({"database_id": 7, "disk_limit_mb": 2048}) is True
    assert fake.calls == [("create", 7, 2048)]


def test_a_database_that_ran_before_never_gets_an_empty_disk_in_place_of_its_own(monkeypatch):
    from dbs import lifecycle
    from dbs.docker import VolumeMissingError
    fake = _FakeQuota(monkeypatch, disk=False)
    with pytest.raises(VolumeMissingError):
        lifecycle._ensure_disk({"database_id": 7, "disk_limit_mb": 2048, "initialized": True})
    assert fake.calls == []


def test_a_bigger_limit_grows_the_disk_and_an_unmounted_one_is_mounted_first(monkeypatch):
    from dbs import lifecycle
    fake = _FakeQuota(monkeypatch, disk=True, mounted=False, size=1024)
    lifecycle._ensure_disk({"database_id": 7, "disk_limit_mb": 4096, "initialized": True})
    assert fake.calls == [("mount", 7), ("grow", 7, 4096)]
    fake = _FakeQuota(monkeypatch, disk=True, mounted=True, size=4096)
    lifecycle._ensure_disk({"database_id": 7, "disk_limit_mb": 4096, "initialized": True})
    assert fake.calls == []                                # as it should be: the helper is not even asked


# ── the API ───────────────────────────────────────────────────────────────────
@pytest.fixture
def database(client):
    from sqlalchemy import delete
    from core.database import AsyncSessionLocal
    from core.env_crypto import encrypt_text
    from core.models import Database
    from nodes.registry import ensure_local_node
    made = []

    def make(name, disk_limit_mb=None, desired_state="stopped"):
        async def go():
            async with AsyncSessionLocal() as db:
                node = await ensure_local_node(db)
                row = Database(name=name, engine="postgres", version="16", node_id=node.id, status="stopped",
                               desired_state=desired_state, disk_limit_mb=disk_limit_mb,
                               credentials=encrypt_text(json.dumps({"user": "u", "password": "p", "db_name": "d"})))
                db.add(row)
                await db.commit()
                made.append(row.id)
                return row.id
        return _run(go())
    yield make

    async def cleanup():
        async with AsyncSessionLocal() as db:
            await db.execute(delete(Database).where(Database.id.in_(made)))
            await db.commit()
    _run(cleanup())


def test_the_limit_is_shown_and_can_only_grow(admin, database):
    db_id = database("disk-grow", disk_limit_mb=1024)
    assert admin.get(f"/api/databases/{db_id}").json()["disk_limit_mb"] == 1024
    assert admin.patch(f"/api/databases/{db_id}", json={"disk_limit_mb": 512}).status_code == 400
    r = admin.patch(f"/api/databases/{db_id}", json={"disk_limit_mb": 4096})
    assert r.status_code == 200 and r.json()["changed"] is True
    assert admin.get(f"/api/databases/{db_id}").json()["disk_limit_mb"] == 4096


def test_a_limit_cannot_be_added_to_a_database_created_without_one(admin, database):
    db_id = database("disk-none")
    r = admin.patch(f"/api/databases/{db_id}", json={"disk_limit_mb": 4096})
    assert r.status_code == 400 and "created" in r.json()["detail"]


def test_creating_with_a_limit_that_is_too_small_is_refused(admin):
    r = admin.post("/api/databases", json={"name": "disk-tiny", "engine": "mysql", "disk_limit_mb": 256})
    assert r.status_code == 400 and "at least" in r.json()["detail"]


def test_the_limit_goes_into_the_spec_the_node_gets():
    from core.models import Database
    from core.env_crypto import encrypt_text
    from dbs.provision import _spec
    row = Database(id=3, name="x", engine="postgres", version="16", disk_limit_mb=2048,
                   credentials=encrypt_text(json.dumps({"user": "u", "password": "p", "db_name": "d"})))
    assert _spec(row)["disk_limit_mb"] == 2048


# ── installation ──────────────────────────────────────────────────────────────
def test_the_setup_script_installs_the_helper_the_client_expects():
    from containers import quota
    setup = (ROOT / "scripts" / "setup-quota.sh").read_text(encoding="utf-8")
    assert f'HELPER="{quota.HELPER}"' in setup and f'DISK_DIR="{quota.DISK_DIR}"' in setup
    assert "NOPASSWD: ${HELPER} *" in setup and "Before=docker.service" in setup and "boot" in setup
    helper_text = SCRIPT.read_text(encoding="utf-8")
    assert f'DISK_DIR = "{quota.DISK_DIR}"' in helper_text


def test_install_and_start_set_it_up():
    install = (ROOT / "install.sh").read_text(encoding="utf-8")
    start = (ROOT / "start.sh").read_text(encoding="utf-8")
    assert "setup-quota.sh" in install and "step_quota" in install
    assert "setup-quota.sh" in start and "quota)" in start
