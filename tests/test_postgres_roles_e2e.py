"""PostgreSQL's users against a real PostgreSQL (dbs/engines.py, PG_ROLES_SCRIPT): the app's user owns its database
but is no superuser, a database from before is switched over without losing anything, and a restore belongs to the
app's user. Runs real containers (cbtest-pg-*), so only when asked:

    CLOUDBASE_E2E=1 python3 tests/test_postgres_roles_e2e.py      # or through pytest

Needs Docker and the image postgres:16 (CLOUDBASE_E2E_PG_IMAGE); it is pulled when missing.
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    import pytest
except ImportError:
    pytest = None

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

IMAGE = os.environ.get("CLOUDBASE_E2E_PG_IMAGE", "postgres:16")
CREDS = {"user": "shop", "password": "app-pass-123", "root_password": "admin-pass-456", "db_name": "shop"}


def can_run() -> bool:
    return os.environ.get("CLOUDBASE_E2E") == "1" and shutil.which("docker") is not None


def docker(*args, check=True, input=None) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, input=input, timeout=300)


def start(name: str, volume: str, env: dict, command: list | None = None) -> None:
    docker("rm", "-f", name, check=False)
    args = ["run", "-d", "--name", name, "-v", f"{volume}:/var/lib/postgresql/data"]
    for k, v in env.items():
        args += ["-e", f"{k}={v}"]
    docker(*args, IMAGE, *(command or []))


def wait(name: str, ready: str, seconds: int = 180) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if docker("exec", name, "sh", "-c", ready, check=False).returncode == 0:
            return
        time.sleep(2)
    raise AssertionError(f"{name} did not get ready: {docker('logs', '--tail', '30', name, check=False).stderr}")


def sql(name: str, user: str, query: str, db: str = "shop") -> subprocess.CompletedProcess:
    """As `user` over TCP with its password, like an app connects."""
    password = CREDS["password"] if user == CREDS["user"] else CREDS["root_password"]
    return docker("exec", "-e", f"PGPASSWORD={password}", name, "psql", "-h", "127.0.0.1", "-U", user, "-d", db,
                  "-tAc", query, check=False)


def new_setup(name: str, volume: str) -> None:
    from dbs import engines
    engine = engines.get_engine("postgres")
    start(name, volume, engine.container_env(CREDS), engine.command(CREDS, None))
    wait(name, engine.healthcheck)


def check_everything() -> list[str]:
    from dbs import engines
    problems = []

    def expect(what, ok):
        print(("  PASS " if ok else "  FAIL ") + what)
        if not ok:
            problems.append(what)

    if docker("image", "inspect", IMAGE, check=False).returncode != 0:
        docker("pull", IMAGE)
    try:
        # ── a new database ──────────────────────────────────────────────────
        new_setup("cbtest-pg-new", "cbtest-pg-new")
        app = CREDS["user"]
        expect("the app's user is no superuser", sql("cbtest-pg-new", app, "select rolsuper from pg_roles where rolname = current_user").stdout.strip() == "f")
        expect("the app's user owns its database", sql("cbtest-pg-new", app, "select pg_get_userbyid(datdba) from pg_database where datname = 'shop'").stdout.strip() == app)
        expect("it can make tables (migrations)", sql("cbtest-pg-new", app, "create table orders (id serial primary key); insert into orders default values; select count(*) from orders").stdout.strip().endswith("1"))
        expect("it can turn on a trusted extension", sql("cbtest-pg-new", app, "create extension if not exists pgcrypto").returncode == 0)
        expect("it can make a test database", sql("cbtest-pg-new", app, "create database shop_test").returncode == 0)
        expect("it cannot run programs (COPY ... PROGRAM)", sql("cbtest-pg-new", app, "copy orders from program 'id'").returncode != 0)
        expect("it cannot read server files", sql("cbtest-pg-new", app, "select pg_read_file('/etc/passwd')").returncode != 0)
        expect("it cannot make roles", sql("cbtest-pg-new", app, "create role sneaky superuser").returncode != 0)
        expect("Cloudbase's admin is a superuser", sql("cbtest-pg-new", engines.PG_ADMIN, "select rolsuper from pg_roles where rolname = current_user").stdout.strip() == "t")

        # ── a database from before: the app's user was the superuser ──────
        start("cbtest-pg-old", "cbtest-pg-old", {"POSTGRES_USER": app, "POSTGRES_PASSWORD": CREDS["password"], "POSTGRES_DB": "shop"})
        wait("cbtest-pg-old", f'pg_isready -q -U {app} -d shop')
        time.sleep(3)
        sql("cbtest-pg-old", app, "create table customers (name text); insert into customers values ('ann')")
        docker("rm", "-f", "cbtest-pg-old")
        new_setup("cbtest-pg-old", "cbtest-pg-old")
        expect("after the switch the app's user is no superuser", sql("cbtest-pg-old", app, "select rolsuper from pg_roles where rolname = current_user").stdout.strip() == "f")
        expect("... its data is still there and still its own", sql("cbtest-pg-old", app, "insert into customers values ('bob'); select count(*) from customers").stdout.strip().endswith("2"))
        expect("... and the admin exists", sql("cbtest-pg-old", engines.PG_ADMIN, "select 1").stdout.strip() == "1")

        # ── a restore belongs to the app's user ───────────────────────────
        dump = docker("exec", "cbtest-pg-new", "sh", "-c", engine_dump() + " > /tmp/b.dump").returncode
        restore = docker("exec", "cbtest-pg-new", "sh", "-c",
                         engines.get_engine("postgres").restore({"path": "/tmp/b.dump", "pg_custom": True, "gzip": False}))
        expect("a backup restores", dump == 0 and restore.returncode == 0)
        expect("... and what it made belongs to the app's user",
               sql("cbtest-pg-new", app, "select tableowner from pg_tables where tablename = 'orders'").stdout.strip() == app)
        expect("... so its migrations can still change it", sql("cbtest-pg-new", app, "alter table orders add column note text").returncode == 0)
    finally:
        for name in ("cbtest-pg-new", "cbtest-pg-old"):
            docker("rm", "-f", name, check=False)
            docker("volume", "rm", name, check=False)
    return problems


def engine_dump() -> str:
    from dbs import engines
    return engines.get_engine("postgres").backup["dump"]


def _needs(function):
    return pytest.mark.skipif(not can_run(), reason="set CLOUDBASE_E2E=1 with Docker to run this")(function) if pytest else function


@_needs
def test_postgres_users_against_a_real_postgres():
    problems = check_everything()
    assert not problems, "\n" + "\n".join(problems)


if __name__ == "__main__":
    if not can_run():
        sys.exit("Needs CLOUDBASE_E2E=1 and Docker:  CLOUDBASE_E2E=1 python3 tests/test_postgres_roles_e2e.py")
    found = check_everything()
    print("all PostgreSQL checks passed" if not found else "\n".join(found))
    sys.exit(1 if found else 0)
