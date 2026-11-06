"""Databases in an export: settings, links and (optionally) data. Docker is replaced by fakes:
the dump is a small file and "starting the container" does nothing, everything else is real."""
import asyncio
import json
import os
import time

import pytest

from core import auth
from test_transfer import (PASSPHRASE, _as_user, _blob, _delete_app, _export, _import, _load_app, _make_app,
                           _manifest_in, _start_export)

FAKE_DUMP = b"PGDMP\x00fake dump of the tx database " + os.urandom(2000)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _reset():
    auth._login_attempts.clear()
    yield
    auth._login_attempts.clear()


def _make_database(name: str, status: str = "running", app_id: int | None = None, engine: str = "postgres") -> int:
    from core.database import AsyncSessionLocal
    from core.env_crypto import encrypt_text
    from core.models import Database, DatabaseLink
    from dbs import engines
    from dbs.provision import _new_credentials, _next_database_id
    from nodes.registry import ensure_local_node

    async def go():
        async with AsyncSessionLocal() as db:
            node = await ensure_local_node(db)
            database = Database(
                id=await _next_database_id(db), name=name, engine=engine, version=engines.get_engine(engine).versions[0],
                node_id=node.id, status=status, desired_state="running", memory_limit_mb=512, cpu_limit=1.5,
                credentials=encrypt_text(json.dumps(_new_credentials(name, engine))),
                backup_schedule="daily", backup_hour=4, backup_retention=7, public_enabled=True)
            db.add(database)
            await db.flush()
            if app_id:
                db.add(DatabaseLink(database_id=database.id, app_id=app_id, env_var_name="DATABASE_URL"))
            await db.commit()
            return database.id
    return _run(go())


def _delete_database(database_id: int) -> None:
    from sqlalchemy import delete
    from core.database import AsyncSessionLocal
    from core.models import Database, DatabaseBackup, DatabaseLink

    async def go():
        async with AsyncSessionLocal() as db:
            await db.execute(delete(DatabaseLink).where(DatabaseLink.database_id == database_id))
            await db.execute(delete(DatabaseBackup).where(DatabaseBackup.database_id == database_id))
            await db.execute(delete(Database).where(Database.id == database_id))
            await db.commit()
    _run(go())


def _database(name: str):
    from sqlalchemy import select
    from core.database import AsyncSessionLocal
    from core.models import Application, Database, DatabaseBackup, DatabaseLink

    async def go():
        async with AsyncSessionLocal() as db:
            row = (await db.execute(select(Database).where(Database.name == name))).scalar_one_or_none()
            if not row:
                return None, [], []
            backups = (await db.execute(select(DatabaseBackup).where(DatabaseBackup.database_id == row.id))).scalars().all()
            links = (await db.execute(select(Application.name, DatabaseLink.env_var_name)
                                      .join(DatabaseLink, DatabaseLink.app_id == Application.id)
                                      .where(DatabaseLink.database_id == row.id))).all()
            return row, backups, [tuple(x) for x in links]
    return _run(go())


@pytest.fixture
def fakes(monkeypatch):
    """No Docker: a dump is a small file, provisioning is skipped, and restores are recorded."""
    from core.database import AsyncSessionLocal
    from core.models import Database, DatabaseBackup
    from dbs import backups as dbs_backups
    from dbs import create as dbs_create
    from transfer import databases as tx_databases
    from transfer import db_import

    calls = {"restores": [], "dumps": [], "provisioned": []}

    async def fake_run_backup(database_id, trigger, actor=None):
        async with AsyncSessionLocal() as db:
            database = await db.get(Database, database_id)
            backup = DatabaseBackup(database_id=database_id, database_name=database.name, engine=database.engine,
                                    version=database.version, source_db_name="tx_source_db", status="success",
                                    trigger=trigger, actor=actor)
            db.add(backup)
            await db.flush()
            backup.file_name = f"{database_id}/fake-{trigger}-{backup.id}.dump"
            path = dbs_backups._path(backup)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(FAKE_DUMP)
            backup.size_bytes = len(FAKE_DUMP)
            await db.commit()
            calls["dumps"].append(database.name)
            return backup.id

    async def fake_restore_when_ready(database_id, backup_id, actor):
        calls["restores"].append((database_id, backup_id))

    monkeypatch.setattr(tx_databases, "run_backup", fake_run_backup)
    monkeypatch.setattr(dbs_create, "_spawn", lambda database_id, fn: calls["provisioned"].append(database_id))
    monkeypatch.setattr(db_import, "_restore_when_ready", fake_restore_when_ready)
    return calls


@pytest.fixture
def deploys(monkeypatch):
    from transfer import importer

    async def nothing(*args, **kwargs):
        return None
    monkeypatch.setattr(importer, "_deploy_app", nothing)
    monkeypatch.setattr(importer, "start_app_now", nothing)


def _tar_of(blob: bytes):
    import io
    import tarfile
    from transfer import crypto
    return tarfile.open(fileobj=io.BytesIO(crypto.decrypt_bytes(blob, PASSPHRASE)), mode="r:")


# ── export ────────────────────────────────────────────────────────────────────
def test_export_holds_settings_and_links_but_no_credentials(admin, fakes):
    app_id = _make_app("txdb-app", instances=1, port=9300)
    database_id = _make_database("txdb-settings", app_id=app_id)
    try:
        r = _export(admin, [app_id], secrets=False, password="", databases=[database_id])
        assert r.status_code == 200 and r.headers["x-database-count"] == "1"
        content = _manifest_in(r.content)
        entry = content["databases"][0]
        assert (entry["name"], entry["engine"], entry["memory_limit_mb"], entry["cpu_limit"]) == ("txdb-settings", "postgres", 512, 1.5)
        assert (entry["backup_schedule"], entry["backup_hour"], entry["backup_retention"]) == ("daily", 4, 7)
        assert entry["links"] == [{"app": "txdb-app", "env_var": "DATABASE_URL"}]
        assert entry["public_was_enabled"] is True and entry["data"] is None
        assert "password" not in json.dumps(content).lower().replace("password_hash", "")
        assert fakes["dumps"] == []                                    # no data was asked for
    finally:
        _delete_database(database_id)
        _delete_app(app_id)


def test_export_with_data_puts_the_dump_in_the_file(admin, fakes):
    database_id = _make_database("txdb-data")
    try:
        r = _export(admin, [], databases=[database_id], data=True, secrets=False, password="")
        assert r.status_code == 200
        with _tar_of(r.content) as tar:
            entry = json.loads(tar.extractfile("manifest.json").read())["databases"][0]
            assert entry["data"]["file"] == "data/0.dump" and entry["data"]["source_db_name"] == "tx_source_db"
            assert tar.extractfile("data/0.dump").read() == FAKE_DUMP
        assert FAKE_DUMP[:20] not in r.content                         # encrypted
        assert fakes["dumps"] == ["txdb-data"]
    finally:
        _delete_database(database_id)


def test_export_of_no_apps_means_no_apps(admin):
    app_id = _make_app("txdb-only-dbs", instances=1, port=9310)
    try:
        assert _manifest_in(_export(admin, [], secrets=False, password="").content)["apps"] == []
        assert [a["name"] for a in _manifest_in(_export(admin, None, secrets=False, password="").content)["apps"]
                if a["name"] == "txdb-only-dbs"] == ["txdb-only-dbs"]            # None: every app
    finally:
        _delete_app(app_id)


def test_data_of_a_stopped_database_cannot_be_exported(admin, fakes):
    database_id = _make_database("txdb-stopped", status="stopped")
    try:
        started = _start_export(admin, [], databases=[database_id], data=True, secrets=False, password="")
        job = started.json()["job_id"]
        for _ in range(200):
            status = admin.get(f"/api/transfer/export/{job}").json()
            if status["state"] != "running":
                break
            time.sleep(0.05)
        assert status["state"] == "failed" and "not running" in status["message"]
        assert admin.get(f"/api/transfer/export/{job}/download").status_code == 404       # no file was left
    finally:
        _delete_database(database_id)


# ── the round trip ────────────────────────────────────────────────────────────
def test_round_trip_recreates_database_links_and_restores_the_data(admin, fakes, deploys):
    app_id = _make_app("txdb-rt-app", instances=1, port=9320)
    database_id = _make_database("txdb-roundtrip", app_id=app_id)
    old, _, _ = _database("txdb-roundtrip")
    from core.env_crypto import decrypt_text
    old_password = json.loads(decrypt_text(old.credentials))["password"]
    blob = _export(admin, [app_id], databases=[database_id], data=True, secrets=False, password="").content
    _delete_database(database_id)
    _delete_app(app_id)

    body = _import(admin, blob).json()
    row, backups, links = _database("txdb-roundtrip")
    app, _ = _load_app("txdb-rt-app")
    try:
        assert body["skipped_databases"] == [] and body["databases"] == [
            {"name": "txdb-roundtrip", "engine": "postgres", "version": row.version, "links": 1, "restoring": True}]
        assert (row.engine, row.memory_limit_mb, row.cpu_limit) == ("postgres", 512, 1.5)
        assert (row.backup_schedule, row.backup_hour, row.backup_retention) == ("daily", 4, 7)
        assert row.public_enabled in (False, None)                       # never switched on by an import
        assert any("reachable from the internet" in w for w in body["warnings"])
        assert json.loads(decrypt_text(row.credentials))["password"] != old_password       # new credentials
        assert links == [("txdb-rt-app", "DATABASE_URL")]
        assert fakes["provisioned"] == [row.id]

        [backup] = backups
        assert (backup.trigger, backup.status, backup.source_db_name) == ("import", "success", "tx_source_db")
        from dbs.backups import _path
        assert open(_path(backup), "rb").read() == FAKE_DUMP
        assert backup.size_bytes == len(FAKE_DUMP) and len(backup.sha256) == 64
        assert fakes["restores"] == [(row.id, backup.id)]                # restore queued for when it runs
    finally:
        _delete_database(row.id)
        _delete_app(app.id)


def test_databases_without_data_are_created_empty(admin, fakes, deploys):
    database_id = _make_database("txdb-empty")
    blob = _export(admin, [], databases=[database_id], secrets=False, password="").content
    _delete_database(database_id)
    body = _import(admin, blob).json()
    row, backups, _ = _database("txdb-empty")
    try:
        assert body["databases"][0]["restoring"] is False
        assert backups == [] and fakes["restores"] == []
    finally:
        _delete_database(row.id)


def test_a_link_to_an_app_that_was_not_imported_becomes_a_warning(admin, fakes, deploys):
    app_id = _make_app("txdb-notimported", instances=1, port=9330)
    database_id = _make_database("txdb-warn", app_id=app_id)
    blob = _export(admin, [], databases=[database_id], secrets=False, password="").content      # no apps in the file
    _delete_database(database_id)
    body = _import(admin, blob).json()
    row, _, links = _database("txdb-warn")
    try:
        assert links == [] and any("not linked to 'txdb-notimported'" in w for w in body["warnings"])
    finally:
        _delete_database(row.id)
        _delete_app(app_id)


def test_existing_names_unknown_engines_and_bad_versions_are_skipped_with_a_reason(admin, fakes):
    existing = _make_database("txdb-exists")
    from transfer import manifest
    entries = [
        {"name": "txdb-exists", "engine": "postgres"},
        {"name": "txdb-nonsense", "engine": "oracle-9000"},
        {"name": "txdb-oldversion", "engine": "postgres", "version": "1.0"},
        {"name": "x", "engine": "postgres"},
    ]
    try:
        body = _import(admin, _blob({"format": manifest.FORMAT, "version": 1, "apps": [], "databases": entries})).json()
        assert body["databases"] == []
        reasons = {s["name"]: s["reason"] for s in body["skipped_databases"]}
        assert "already exists" in reasons["txdb-exists"]
        assert reasons["txdb-nonsense"]
        assert "not offered" in reasons["txdb-oldversion"]
        assert "3–32 characters" in reasons["x"]
        assert _database("txdb-nonsense")[0] is None and _database("txdb-oldversion")[0] is None
    finally:
        _delete_database(existing)


def test_a_data_path_that_points_outside_the_archive_creates_nothing(admin, fakes):
    from transfer import manifest
    entry = {"name": "txdb-evil", "engine": "postgres", "data": {"file": "data/../../../etc/passwd"}}
    body = _import(admin, _blob({"format": manifest.FORMAT, "version": 1, "apps": [], "databases": [entry]})).json()
    assert body["databases"] == [] and body["skipped_databases"][0]["name"] == "txdb-evil"
    assert _database("txdb-evil")[0] is None                              # refused before anything was created


# ── permissions ───────────────────────────────────────────────────────────────
def test_data_needs_the_backup_permission(admin, fakes):
    database_id = _make_database("txdb-perm")
    try:
        with _as_user(admin, ["apps.view", "apps.configure", "databases.view"]) as user:
            r = _start_export(user, [], databases=[database_id], data=True, secrets=False, password="")
            assert r.status_code == 403 and "databases.backup" in r.json()["detail"]
            assert _export(user, [], databases=[database_id], secrets=False, password="").status_code == 200   # settings only
        with _as_user(admin, ["apps.view", "databases.backup"]) as user:                 # data, but not allowed to see databases
            assert _start_export(user, [], databases=[database_id], secrets=False, password="").status_code == 403
    finally:
        _delete_database(database_id)


def test_importing_databases_needs_the_create_permission_and_data_needs_backup(admin, fakes, deploys):
    database_id = _make_database("txdb-perm-rt")
    blob = _export(admin, [], databases=[database_id], data=True, secrets=False, password="").content
    _delete_database(database_id)
    try:
        with _as_user(admin, ["apps.view", "apps.deploy"]) as user:
            body = _import(user, blob).json()
            assert body["databases"] == [] and "databases.create" in body["skipped_databases"][0]["reason"]
        with _as_user(admin, ["apps.view", "databases.create"]) as user:
            body = _import(user, blob).json()
            assert [d["name"] for d in body["databases"]] == ["txdb-perm-rt"] and body["databases"][0]["restoring"] is False
            assert any("databases.backup" in w for w in body["warnings"]) and fakes["restores"] == []
    finally:
        row, _, _ = _database("txdb-perm-rt")
        if row:
            _delete_database(row.id)


# ── the create endpoint still works after the refactor ─────────────────────────
def test_the_create_endpoint_still_creates_a_database(admin, fakes):
    r = admin.post("/api/databases", json={"name": "txdb-endpoint", "engine": "postgres"})
    try:
        assert r.status_code == 200, r.text
        row, _, _ = _database("txdb-endpoint")
        assert row.status == "creating" and fakes["provisioned"] == [row.id]
        assert admin.post("/api/databases", json={"name": "txdb-endpoint", "engine": "postgres"}).status_code in (400, 409)
        assert admin.post("/api/databases", json={"name": "Bad Name", "engine": "postgres"}).status_code == 400
    finally:
        row, _, _ = _database("txdb-endpoint")
        if row:
            _delete_database(row.id)


# ── jobs ──────────────────────────────────────────────────────────────────────
def test_an_export_can_only_be_downloaded_once_and_only_by_its_owner(admin):
    job = _start_export(admin, [], secrets=False, password="").json()["job_id"]
    for _ in range(200):
        if admin.get(f"/api/transfer/export/{job}").json()["state"] != "running":
            break
        time.sleep(0.05)
    with _as_user(admin, ["apps.view"]) as other:
        assert other.get(f"/api/transfer/export/{job}").status_code == 404
        assert other.get(f"/api/transfer/export/{job}/download").status_code == 404
    first = admin.get(f"/api/transfer/export/{job}/download")
    assert first.status_code == 200 and len(first.content) > 100
    assert admin.get(f"/api/transfer/export/{job}/download").status_code == 404             # gone after one download
    assert admin.get(f"/api/transfer/export/{job}").status_code == 404


def test_old_export_files_are_swept_away(tmp_path, monkeypatch):
    from transfer import jobs
    monkeypatch.setattr(jobs, "EXPORT_DIR", str(tmp_path))
    jobs._ensure_dir()
    old, fresh = tmp_path / "old.cloudbase", tmp_path / "fresh.cloudbase"
    old.write_bytes(b"x")
    fresh.write_bytes(b"x")
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    jobs.sweep()
    assert not old.exists() and fresh.exists()


def test_no_unencrypted_archive_is_left_behind(admin):
    from transfer import jobs
    _export(admin, [], secrets=False, password="")
    assert not [f for f in os.listdir(jobs.EXPORT_DIR) if f.endswith(".tar")]
