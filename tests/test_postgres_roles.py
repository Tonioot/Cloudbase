"""PostgreSQL's app user owns its database but is no superuser (dbs/engines.py, PG_ROLES_SCRIPT). These check what
Cloudbase hands Docker; tests/test_postgres_roles_e2e.py checks the same against a real PostgreSQL."""
import asyncio
import json

import pytest

CREDS = {"user": "shop", "password": "app-pass", "root_password": "admin-pass", "db_name": "shop"}


def _run(coro):
    return asyncio.run(coro)


def test_the_superuser_is_cloudbases_and_the_app_user_comes_in_beside_it():
    from dbs import engines
    env = engines.get_engine("postgres").container_env(CREDS)
    assert env["POSTGRES_USER"] == engines.PG_ADMIN and env["POSTGRES_PASSWORD"] == "admin-pass"
    assert env["CB_APP_USER"] == "shop" and env["CB_APP_PASSWORD"] == "app-pass" and env["POSTGRES_DB"] == "shop"


def test_the_app_user_gets_ownership_and_test_databases_but_none_of_the_dangerous_rights():
    from dbs import engines
    sql = engines.PG_ROLES_SQL
    assert "NOSUPERUSER" in sql and "NOCREATEROLE" in sql and "NOREPLICATION" in sql and "NOBYPASSRLS" in sql
    assert "CREATEDB" in sql and 'ALTER DATABASE :"db" OWNER TO :"app"' in sql and "ALTER SCHEMA public OWNER" in sql
    assert "pg_execute_server_program" not in sql and "pg_read_server_files" not in sql


def test_the_server_starts_through_the_roles_script_with_its_tuning():
    from dbs import engines
    engine = engines.get_engine("postgres")
    command = engine.command(CREDS, 1024)
    assert command[:3] == ["sh", "-c", engines.PG_ROLES_SCRIPT] and command[3] == "sh"
    assert command[4:] == ["postgres", "-c", "shared_buffers=256MB", "-c", "effective_cache_size=768MB"]
    assert engine.command(CREDS, None)[4:] == ["postgres"]
    assert 'exec docker-entrypoint.sh "$@"' in engines.PG_ROLES_SCRIPT
    # it switches a database from before over: makes the admin with the old superuser (the app user) first
    assert '-U "$CB_APP_USER"' in engines.PG_ROLES_SCRIPT and "SUPERUSER LOGIN" in engines.PG_ROLES_SCRIPT


def test_it_only_counts_as_running_once_the_roles_are_set():
    from dbs import engines
    assert engines.get_engine("postgres").healthcheck.endswith(f"test -f {engines.PG_ROLES_DONE}")


def test_a_restore_belongs_to_the_app_user():
    from dbs import engines
    restore = engines.get_engine("postgres").restore
    custom = restore({"path": "/tmp/b.dump", "pg_custom": True, "gzip": False})
    plain = restore({"path": "/tmp/b.sql", "pg_custom": False, "gzip": False})
    assert '--role="${CB_APP_USER:-$POSTGRES_USER}"' in custom and "--no-owner" in custom
    assert '-c "SET ROLE "${CB_APP_USER:-$POSTGRES_USER}""' in plain and plain.index("SET ROLE") < plain.index("-f ")


def test_no_database_can_be_named_after_the_admin():
    from dbs import engines
    assert "cloudbase-admin" in engines.RESERVED_NAMES


def test_a_new_password_reaches_the_container_too():
    from dbs import backup_restore
    assert "postgres" in backup_restore.ROTATE_RECREATES        # the roles script sets it from the environment


class _Container:
    def __init__(self, labels):
        self.labels = labels


def test_a_container_from_before_the_roles_is_replaced_once():
    from dbs import lifecycle
    assert lifecycle._outdated(_Container({"cloudbase.engine": "postgres"}))
    assert not lifecycle._outdated(_Container({"cloudbase.engine": "postgres", "cloudbase.setup": "roles-1"}))
    assert not lifecycle._outdated(_Container({"cloudbase.engine": "mysql"}))      # nothing changed for the others


def test_a_nodes_report_says_which_containers_are_outdated():
    from nodes import reports
    reports.store_databases(424242, {"7": {"state": "running", "volume": True, "outdated": True}})
    assert reports.databases(424242)[1]["7"]["outdated"] is True


def test_a_database_gets_the_sandbox_only_when_asked(monkeypatch):
    from core import config, tenant
    base = {**config._DEFAULTS["tenant"], "enabled": True, "runtime": "runsc"}
    monkeypatch.setattr(config, "get_tenant", lambda: base)
    assert tenant.hardening()["runtime"] == "runsc" and tenant.hardening(database=True)["runtime"] == ""
    monkeypatch.setattr(config, "get_tenant", lambda: {**base, "sandbox_databases": True})
    assert tenant.hardening(database=True)["runtime"] == "runsc"


@pytest.fixture
def pg_database(client):
    from sqlalchemy import delete
    from core.database import AsyncSessionLocal
    from core.env_crypto import encrypt_text
    from core.models import Database
    from nodes.registry import ensure_local_node
    made = []

    async def make():
        async with AsyncSessionLocal() as db:
            node = await ensure_local_node(db)
            row = Database(name="pg-roles", engine="postgres", version="16", node_id=node.id, status="stopped",
                           desired_state="stopped", credentials=encrypt_text(json.dumps(CREDS)))
            db.add(row)
            await db.commit()
            made.append(row.id)
            return row.id
    yield _run(make())

    async def cleanup():
        async with AsyncSessionLocal() as db:
            await db.execute(delete(Database).where(Database.id.in_(made)))
            await db.commit()
    _run(cleanup())


def test_the_admin_login_is_yours_but_never_a_customers(admin, pg_database, monkeypatch):
    from core import config
    from dbs import engines
    own = admin.get(f"/api/databases/{pg_database}/credentials").json()
    assert own["user"] == "shop" and own["root_user"] == engines.PG_ADMIN and own["root_password"] == "admin-pass"
    monkeypatch.setattr(config, "get_tenant", lambda: {**config._DEFAULTS["tenant"], "enabled": True})
    tenants = admin.get(f"/api/databases/{pg_database}/credentials").json()
    assert "root_password" not in tenants and "root_user" not in tenants and tenants["password"] == "app-pass"
