"""TEMPORARY, goes together with backend/rename_migration.py: the rename of "replica" to "instance"."""
import asyncio
import json
import pathlib


import rename_migration

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _run(coro):
    return asyncio.run(coro)


OLD_SCHEMA = [
    "CREATE TABLE applications (id INTEGER PRIMARY KEY, name VARCHAR(100), autoscale_min_replicas INTEGER, autoscale_max_replicas INTEGER)",
    "CREATE TABLE nodes (id INTEGER PRIMARY KEY, name VARCHAR(120), is_local BOOLEAN)",
    "CREATE TABLE application_replicas (id INTEGER PRIMARY KEY, app_id INTEGER, node_id INTEGER, status VARCHAR(20), "
    "desired VARCHAR(10), spec_json TEXT, spec_hash VARCHAR(20), external_port INTEGER)",
    "CREATE INDEX ix_application_replicas_app_id ON application_replicas (app_id)",
    "CREATE INDEX ix_application_replicas_node_id ON application_replicas (node_id)",
    "CREATE INDEX ix_application_replicas_id ON application_replicas (id)",
]


def _engine(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine
    return create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")


def test_an_old_database_gets_the_new_names_and_keeps_its_data(tmp_path):
    from core.database import Base
    from core.env_crypto import decrypt_text, encrypt_text
    import core.models  # noqa: F401  (registers every table for create_all)
    engine = _engine(tmp_path)

    async def go():
        async with engine.begin() as conn:
            for sql in OLD_SCHEMA:
                await conn.exec_driver_sql(sql)
            await conn.exec_driver_sql("INSERT INTO applications VALUES (1, 'shop', 2, 6)")
            await conn.exec_driver_sql("INSERT INTO nodes VALUES (1, 'primary', 1)")
            spec = encrypt_text(json.dumps({"replica_id": 7, "app_id": 1, "external_port": 9100}))
            await conn.exec_driver_sql("INSERT INTO application_replicas VALUES (7, 1, 1, 'running', 'running', ?, 'abc123', 9100)", (spec,))
            await conn.exec_driver_sql("INSERT INTO application_replicas VALUES (8, 1, 1, 'stopped', 'stopped', NULL, NULL, 9101)")
        for _ in range(2):                                         # a second start changes nothing
            async with engine.begin() as conn:
                await rename_migration.migrate_database(conn)
                await conn.run_sync(Base.metadata.create_all)
        async with engine.connect() as conn:
            tables = {r[0] for r in (await conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'table'")).fetchall()}
            indexes = {r[0] for r in (await conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'index'")).fetchall()}
            rows = (await conn.exec_driver_sql("SELECT id, status, spec_json, spec_hash FROM application_instances ORDER BY id")).fetchall()
            app = (await conn.exec_driver_sql("SELECT autoscale_min_instances, autoscale_max_instances FROM applications")).fetchone()
        await engine.dispose()
        return tables, indexes, rows, app

    tables, indexes, rows, app = _run(go())
    assert "application_instances" in tables and "application_replicas" not in tables
    assert not [i for i in indexes if "replicas" in i]
    assert [(r[0], r[1]) for r in rows] == [(7, "running"), (8, "stopped")]
    spec = json.loads(decrypt_text(rows[0][2]))
    assert spec == {"instance_id": 7, "app_id": 1, "external_port": 9100}
    assert rows[0][3] == "abc123"                                  # same hash: no node recreates the container
    assert tuple(app) == (2, 6)


def test_a_new_database_is_left_alone(tmp_path):
    from core.database import Base
    import core.models  # noqa: F401
    engine = _engine(tmp_path)

    async def go():
        async with engine.begin() as conn:
            await rename_migration.migrate_database(conn)
            await conn.run_sync(Base.metadata.create_all)
            await rename_migration.migrate_database(conn)
            tables = {r[0] for r in (await conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'table'")).fetchall()}
        await engine.dispose()
        return tables
    tables = _run(go())
    assert "application_instances" in tables and "application_replicas" not in tables


class _Container:
    def __init__(self, name):
        self.name = name

    def rename(self, new):
        self.name = new


class _Client:
    def __init__(self, names):
        self.items = [_Container(n) for n in names]
        self.containers = self

    def list(self, all=False):
        return self.items


def test_running_containers_get_their_new_name_once(monkeypatch):
    monkeypatch.setattr(rename_migration, "_containers_done", False)
    client = _Client(["cloudbase-app-3-replica-12", "cloudbase-app-3-instance-13", "cloudbase-job-3-9", "postgres"])
    rename_migration.rename_old_containers(client)
    assert [c.name for c in client.items] == ["cloudbase-app-3-instance-12", "cloudbase-app-3-instance-13", "cloudbase-job-3-9", "postgres"]
    late = _Client(["cloudbase-app-4-replica-1"])
    rename_migration.rename_old_containers(late)                   # once per process: Docker is not asked again
    assert late.items[0].name == "cloudbase-app-4-replica-1"


def test_the_reconciler_sees_renamed_containers(monkeypatch):
    from containers import instances
    monkeypatch.setattr(rename_migration, "_containers_done", False)

    class C(_Container):
        id, status, labels, attrs = "c1", "running", {"cloudbase.spec": "abc123"}, {"State": {"ExitCode": 0}}
    client = _Client([])
    client.items = [C("cloudbase-app-3-replica-12")]
    monkeypatch.setattr(instances, "_get_client", lambda: client)
    found = instances.list_instance_containers()
    assert [(c["app_id"], c["instance_id"], c["spec"]) for c in found] == [(3, 12, "abc123")]


def test_an_export_from_before_the_rename_keeps_its_autoscale_settings():
    old = {"name": "shop", "autoscale_min_replicas": 2, "autoscale_max_replicas": 5}
    assert rename_migration.old_export_keys(old) == {"name": "shop", "autoscale_min_instances": 2, "autoscale_max_instances": 5}
    both = {"autoscale_min_replicas": 2, "autoscale_min_instances": 3}
    assert rename_migration.old_export_keys(both)["autoscale_min_instances"] == 3


def test_an_agent_from_before_the_rename_gets_no_list(client):
    """It would misread it; without a list it removes nothing, so its containers keep running until it is updated."""
    from sqlalchemy import delete
    from core.database import AsyncSessionLocal
    from core.models import Node

    async def make(version):
        async with AsyncSessionLocal() as db:
            node = Node(name=f"rename-{version}", status="online", auth_token=f"token-rename-{version}", is_local=False,
                        enabled=True, agent_version=version)
            db.add(node)
            await db.commit()
            return node.id
    ids = [_run(make("2.0.0")), _run(make("3.0.0"))]
    try:
        old = client.get("/api/nodes/agent/desired", headers={"X-Node-Token": "token-rename-2.0.0"})
        assert old.status_code == 409 and "cloudbase update" in old.json()["detail"]
        new = client.get("/api/nodes/agent/desired", headers={"X-Node-Token": "token-rename-3.0.0"})
        assert new.status_code == 200 and "instances" in new.json()
    finally:
        async def cleanup():
            async with AsyncSessionLocal() as db:
                await db.execute(delete(Node).where(Node.id.in_(ids)))
                await db.commit()
        _run(cleanup())


ALLOWED = {ROOT / "backend" / "rename_migration.py", ROOT / "tests" / "test_rename_migration.py"}
SKIP_DIRS = {"__pycache__", "venv", ".venv", ".git", "node_modules", ".dev"}
TEXT = {".py", ".js", ".html", ".css", ".md", ".sh", ".yaml", ".yml", ".txt", ".json", ".toml"}


CODE = [ROOT / d for d in ("backend", "frontend", "tests", "scripts")] + [ROOT / f for f in ("start.sh", "install.sh", "config.yaml")]


def test_the_old_name_is_nowhere_else():
    """Everything says instance; only the temporary migration knows the old name. (ROADMAP's database
    replication is another thing.)"""
    found = []
    for path in [p for root in CODE for p in ([root] if root.is_file() else root.rglob("*"))]:
        if path.is_dir() or path in ALLOWED or SKIP_DIRS & set(path.parts) or path.suffix not in TEXT:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if "replica" in line.lower():
                found.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()[:100]}")
    assert not found, "\n".join(found)
