"""Managed database engines: everything engine-specific lives here, as plain data.

Each engine describes its official Docker image, the versions offered in the panel,
how the container is configured (environment, command, healthcheck, data directory)
and how apps connect to it. Memory-based tuning is derived from the container's
memory limit so a database performs well without any manual configuration.

Credentials are a dict with `user`, `password` and `db_name`.
"""

from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class Engine:
    key: str
    label: str
    image: str                 # image repository; the tag is the version
    versions: tuple[str, ...]  # newest first; the first one is the default
    port: int
    env_var: str               # default env var name when linking to an app
    container_env: Callable[[dict], dict]
    command: Callable[[dict, Optional[int]], Optional[list[str]]]  # (credentials, memory_limit_mb)
    healthcheck: str           # CMD-SHELL test; secrets come from the container env
    data_dir: Callable[[str], str]
    url: Callable[[dict, str, int], str]  # (credentials, host, port)
    # Backups run inside the container with `sh -c`; secrets come from its env.
    # backup: {"ext", "dump" (writes the dump to stdout), "gzip" (compress it here)}
    backup: Optional[dict] = None
    # restore(ctx) -> shell command restoring ctx["path"]; None = not supported yet.
    # ctx: path, gzip (file is gzipped), pg_custom (pg_dump -Fc), ns_from / ns_to (database names)
    restore: Optional[Callable[[dict], str]] = None
    # Turn a public URL into one that uses TLS (the public port requires it by default).
    tls_url: Optional[Callable[[str], str]] = None
    # Same, but also verifying the server against the Cloudbase CA file (None when the
    # engine encrypts with its own certificate, as MySQL/MariaDB do).
    tls_verify_url: Optional[Callable[[str], str]] = None
    # Restore with a helper container instead of a command in the running one
    # (Redis / Valkey load their data only at startup).
    restore_via_helper: Optional[dict] = None  # {"server": ..., "cli": ...}
    # Prints one number (tables, collections, keys) to compare a database before and after it moves.
    count: Optional[str] = None
    count_approximate: bool = False  # keys expire, so the count may drop a little by itself

    @property
    def can_restore(self) -> bool:
        return self.restore is not None or self.restore_via_helper is not None

    def image_ref(self, version: str) -> str:
        return f"{self.image}:{version}"


# ── PostgreSQL: the app gets an owner, not a superuser ────────────────────────
# The image makes POSTGRES_USER a superuser, and a superuser can run programs on the server (COPY ... PROGRAM). So
# the superuser is Cloudbase's own (cloudbase_admin, with the database's root_password), and the user apps get owns
# its database and nothing more: tables, migrations, trusted extensions (pgcrypto, pg_trgm, citext, ...) and CREATEDB
# (test databases), but no programs, no server files, no CREATEROLE (up to PostgreSQL 15 that leads back to running
# programs). A script next to the server sets this up at every start, so a database from before (whose app user is
# the superuser) is switched over too: it makes the admin with the old superuser first. The health check waits for
# it: a database whose roles could not be set never shows as running.
PG_ADMIN = "cloudbase_admin"
PG_ROLES_DONE = "/tmp/cloudbase-roles-ok"
SETUP_VERSION = {"postgres": "roles-1"}      # a container made before (label cloudbase.setup) is replaced once

PG_ROLES_SQL = r"""SELECT format('CREATE ROLE %I LOGIN', :'app') WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app') \gexec
ALTER ROLE :"app" NOSUPERUSER NOCREATEROLE NOREPLICATION NOBYPASSRLS CREATEDB LOGIN PASSWORD :'pw';
ALTER DATABASE :"db" OWNER TO :"app";
ALTER SCHEMA public OWNER TO :"app";
"""

PG_ROLES_SCRIPT = r"""unlink /tmp/cloudbase-roles-ok 2>/dev/null
(
  i=0
  while [ ! -f /tmp/cloudbase-roles-ok ] && [ $i -lt 1800 ]; do
    sleep 2; i=$((i+2))
    pg_isready -q || continue
    if ! psql -qtA -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select 1" >/dev/null 2>&1; then
      echo "CREATE ROLE :\"admin\" SUPERUSER LOGIN PASSWORD :'pw';" | psql -q -v ON_ERROR_STOP=1 \
        -v admin="$POSTGRES_USER" -v pw="$POSTGRES_PASSWORD" -U "$CB_APP_USER" -d "$POSTGRES_DB" >/dev/null 2>&1 || continue
    fi
    printf '%s\n' "$CB_ROLES_SQL" | psql -q -v ON_ERROR_STOP=1 -v app="$CB_APP_USER" -v db="$POSTGRES_DB" \
      -v pw="$CB_APP_PASSWORD" -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1 && touch /tmp/cloudbase-roles-ok
  done
) &
exec docker-entrypoint.sh "$@"
"""


def _postgres_env(c: dict) -> dict:
    return {
        "POSTGRES_USER": PG_ADMIN,
        "POSTGRES_PASSWORD": c["root_password"],
        "POSTGRES_DB": c["db_name"],
        "CB_APP_USER": c["user"],
        "CB_APP_PASSWORD": c["password"],
        "CB_ROLES_SQL": PG_ROLES_SQL,
    }


# ── Tuning helpers ────────────────────────────────────────────────────────────

def _postgres_command(_creds: dict, mem_mb: Optional[int]) -> list[str]:
    server = ["postgres"]
    if mem_mb:
        server += [
            "-c", f"shared_buffers={max(mem_mb // 4, 32)}MB",
            "-c", f"effective_cache_size={max(mem_mb * 3 // 4, 64)}MB",
        ]
    # sh -c runs the roles script beside the image's own entrypoint, which then starts the server as usual
    return ["sh", "-c", PG_ROLES_SCRIPT, "sh", *server]


def _innodb_command(_creds: dict, mem_mb: Optional[int]) -> Optional[list[str]]:
    # Arguments starting with "-" are appended to mysqld/mariadbd by the image entrypoint.
    if not mem_mb:
        return None
    return [f"--innodb-buffer-pool-size={max(mem_mb // 2, 16)}M"]


def _redis_command(server: str):
    def build(creds: dict, mem_mb: Optional[int]) -> list[str]:
        cmd = [server, "--requirepass", creds["password"], "--appendonly", "yes"]
        if mem_mb:
            cmd += ["--maxmemory", f"{max(mem_mb * 4 // 5, 16)}mb", "--maxmemory-policy", "noeviction"]
        return cmd
    return build


def _mongo_command(_creds: dict, mem_mb: Optional[int]) -> Optional[list[str]]:
    if not mem_mb:
        return None
    # MongoDB's own default: 50% of (RAM - 1 GB), at least 0.25 GB.
    cache_gb = max(0.25, round((mem_mb - 1024) / 1024 * 0.5, 2))
    return ["mongod", "--wiredTigerCacheSizeGB", str(cache_gb)]


def _postgres_data_dir(version: str) -> str:
    # From PostgreSQL 18 the image keeps PGDATA in a versioned subdirectory and
    # declares /var/lib/postgresql itself as the volume.
    try:
        major = int(version.split(".")[0])
    except ValueError:
        major = 0
    return "/var/lib/postgresql" if major >= 18 else "/var/lib/postgresql/data"


# ── Backup commands ───────────────────────────────────────────────────────────

# What a restore creates belongs to the app user (the database's owner), not to the admin that runs it: otherwise the
# app's migrations could no longer change its own tables. (A container from before has no CB_APP_USER: its
# POSTGRES_USER is the app user then.)
_PG_APP = '"${CB_APP_USER:-$POSTGRES_USER}"'


def _pg_restore(ctx: dict) -> str:
    if ctx["pg_custom"]:
        return (f'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists '
                f'--no-owner --no-privileges --role={_PG_APP} {ctx["path"]}')
    return (f'psql -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" '
            f'-c "SET ROLE {_PG_APP}" -f {ctx["path"]}')


_MONGO_AUTH = '--username "$MONGO_INITDB_ROOT_USERNAME" --password "$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase admin'
# Never restore users and internals: restoring admin.* with --drop would replace
# this database's own login. (mongodump has no --nsExclude, so dumps include them
# and only the restore leaves them out.)
_MONGO_SYSTEM = '--nsExclude "admin.*" --nsExclude "config.*" --nsExclude "local.*"'


def _mongo_restore(ctx: dict) -> str:
    cmd = f'mongorestore --quiet --drop --archive={ctx["path"]} {_MONGO_AUTH} {_MONGO_SYSTEM}'
    if ctx["gzip"]:
        cmd += " --gzip"
    if ctx.get("ns_from") and ctx.get("ns_to") and ctx["ns_from"] != ctx["ns_to"]:
        # A backup of another database: move its data into this one's database name.
        cmd += f' --nsFrom "{ctx["ns_from"]}.*" --nsTo "{ctx["ns_to"]}.*"'
    return cmd


def _redis_dump(cli: str) -> str:
    return (f"{cli} --rdb /tmp/cb-backup.rdb >&2 && cat /tmp/cb-backup.rdb; "
            "rc=$?; rm -f /tmp/cb-backup.rdb; exit $rc")


def _with_param(url: str, param: str) -> str:
    return url + ("&" if "?" in url else "?") + param


# ── Registry ──────────────────────────────────────────────────────────────────

ENGINES: dict[str, Engine] = {
    "postgres": Engine(
        key="postgres",
        label="PostgreSQL",
        image="postgres",
        versions=("18", "17", "16", "15"),
        port=5432,
        env_var="DATABASE_URL",
        container_env=_postgres_env,
        command=_postgres_command,
        # running = accepting connections and the roles are set (see PG_ROLES_SCRIPT)
        healthcheck=f'pg_isready -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" && test -f {PG_ROLES_DONE}',
        data_dir=_postgres_data_dir,
        url=lambda c, host, port: f"postgresql://{c['user']}:{c['password']}@{host}:{port}/{c['db_name']}",
        backup={"ext": "dump", "gzip": False,
                "dump": 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --no-owner'},
        restore=_pg_restore,
        count='psql -tA -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select count(*) from information_schema.tables where table_schema not in (\x27pg_catalog\x27,\x27information_schema\x27)"',
        tls_url=lambda u: _with_param(u, "sslmode=require"),
        tls_verify_url=lambda u: _with_param(u, "sslmode=verify-full&sslrootcert=cloudbase-database-ca.crt"),
    ),
    "mysql": Engine(
        key="mysql",
        label="MySQL",
        image="mysql",
        versions=("8.4", "9"),
        port=3306,
        env_var="DATABASE_URL",
        container_env=lambda c: {
            "MYSQL_ROOT_PASSWORD": c["root_password"],
            "MYSQL_DATABASE": c["db_name"],
            "MYSQL_USER": c["user"],
            "MYSQL_PASSWORD": c["password"],
        },
        command=_innodb_command,
        # 127.0.0.1 only answers once the real server runs (init uses --skip-networking).
        healthcheck='MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysqladmin ping -h 127.0.0.1 -uroot --silent',
        data_dir=lambda _v: "/var/lib/mysql",
        url=lambda c, host, port: f"mysql://{c['user']}:{c['password']}@{host}:{port}/{c['db_name']}",
        backup={"ext": "sql.gz", "gzip": True,
                "dump": 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysqldump -uroot --single-transaction --routines '
                        '--triggers --events --set-gtid-purged=OFF "$MYSQL_DATABASE"'},
        restore=lambda ctx: f'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot "$MYSQL_DATABASE" < {ctx["path"]}',
        count='MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot -N -B -e "select count(*) from information_schema.tables where table_schema=\x27$MYSQL_DATABASE\x27"',
        tls_url=lambda u: _with_param(u, "ssl-mode=REQUIRED"),
    ),
    "mariadb": Engine(
        key="mariadb",
        label="MariaDB",
        image="mariadb",
        versions=("11.8", "11.4", "10.11"),
        port=3306,
        env_var="DATABASE_URL",
        container_env=lambda c: {
            "MARIADB_ROOT_PASSWORD": c["root_password"],
            "MARIADB_DATABASE": c["db_name"],
            "MARIADB_USER": c["user"],
            "MARIADB_PASSWORD": c["password"],
        },
        command=_innodb_command,
        healthcheck="healthcheck.sh --connect --innodb_initialized",
        data_dir=lambda _v: "/var/lib/mysql",
        url=lambda c, host, port: f"mysql://{c['user']}:{c['password']}@{host}:{port}/{c['db_name']}",
        backup={"ext": "sql.gz", "gzip": True,
                "dump": 'MYSQL_PWD="$MARIADB_ROOT_PASSWORD" mariadb-dump -uroot --single-transaction --routines '
                        '--triggers --events "$MARIADB_DATABASE"'},
        restore=lambda ctx: f'MYSQL_PWD="$MARIADB_ROOT_PASSWORD" mariadb -uroot "$MARIADB_DATABASE" < {ctx["path"]}',
        count='MYSQL_PWD="$MARIADB_ROOT_PASSWORD" mariadb -uroot -N -B -e "select count(*) from information_schema.tables where table_schema=\x27$MARIADB_DATABASE\x27"',
        tls_url=lambda u: _with_param(u, "ssl-mode=REQUIRED"),
    ),
    "redis": Engine(
        key="redis",
        label="Redis",
        image="redis",
        versions=("8", "7.4"),
        port=6379,
        env_var="REDIS_URL",
        container_env=lambda c: {"REDISCLI_AUTH": c["password"]},
        command=_redis_command("redis-server"),
        healthcheck="redis-cli ping | grep -q PONG",
        data_dir=lambda _v: "/data",
        url=lambda c, host, port: f"redis://default:{c['password']}@{host}:{port}/0",
        backup={"ext": "rdb.gz", "gzip": True, "dump": _redis_dump("redis-cli")},
        restore_via_helper={"server": "redis-server", "cli": "redis-cli"},
        count="redis-cli dbsize", count_approximate=True,
        tls_url=lambda u: "rediss://" + u.split("://", 1)[1],
        tls_verify_url=lambda u: "rediss://" + u.split("://", 1)[1],  # pass the CA to the client (--cacert)
    ),
    "valkey": Engine(
        key="valkey",
        label="Valkey",
        image="valkey/valkey",
        versions=("9", "8"),
        port=6379,
        env_var="REDIS_URL",
        container_env=lambda c: {"VALKEYCLI_AUTH": c["password"], "REDISCLI_AUTH": c["password"]},
        command=_redis_command("valkey-server"),
        healthcheck="valkey-cli ping | grep -q PONG",
        data_dir=lambda _v: "/data",
        url=lambda c, host, port: f"redis://default:{c['password']}@{host}:{port}/0",
        backup={"ext": "rdb.gz", "gzip": True, "dump": _redis_dump("valkey-cli")},
        restore_via_helper={"server": "valkey-server", "cli": "valkey-cli"},
        count="valkey-cli dbsize", count_approximate=True,
        tls_url=lambda u: "rediss://" + u.split("://", 1)[1],
        tls_verify_url=lambda u: "rediss://" + u.split("://", 1)[1],  # pass the CA to the client (--cacert)
    ),
    "mongodb": Engine(
        key="mongodb",
        label="MongoDB",
        image="mongo",
        versions=("8.0", "7.0"),
        port=27017,
        env_var="MONGODB_URL",
        container_env=lambda c: {
            "MONGO_INITDB_ROOT_USERNAME": c["user"],
            "MONGO_INITDB_ROOT_PASSWORD": c["password"],
            "MONGO_INITDB_DATABASE": c["db_name"],
        },
        command=_mongo_command,
        healthcheck="mongosh --quiet --eval \"db.adminCommand('ping').ok\" | grep -q 1",
        data_dir=lambda _v: "/data/db",
        url=lambda c, host, port: f"mongodb://{c['user']}:{c['password']}@{host}:{port}/{c['db_name']}?authSource=admin",
        backup={"ext": "archive.gz", "gzip": False,
                "dump": f"mongodump --quiet --archive --gzip {_MONGO_AUTH}"},
        restore=_mongo_restore,
        count=f'mongosh --quiet {_MONGO_AUTH} "$MONGO_INITDB_DATABASE" --eval "db.getCollectionNames().length"',
        # Self-signed by the Cloudbase CA: encrypt without verifying, or use tlsCAFile.
        tls_url=lambda u: _with_param(u, "tls=true&tlsAllowInvalidCertificates=true"),
        tls_verify_url=lambda u: _with_param(u, "tls=true&tlsCAFile=cloudbase-database-ca.crt"),
    ),
}

# Names that collide with built-in users or databases of one of the engines.
RESERVED_NAMES = {
    "root", "admin", "mysql", "mariadb", "postgres", "template0", "template1",
    "sys", "information-schema", "performance-schema", "local", "config", "test", "default",
    "cloudbase-admin",          # its user would be PG_ADMIN
}


def setup_version(engine: str) -> str:
    """What a database's container is set up for (label cloudbase.setup); another one is replaced once."""
    return SETUP_VERSION.get(engine, "")


def get_engine(key: str) -> Engine:
    engine = ENGINES.get(key)
    if engine is None:
        raise ValueError(f"Unknown database engine: {key!r}")
    return engine


def catalog() -> list[dict]:
    """Engines and versions for the panel."""
    return [
        {
            "key": e.key,
            "label": e.label,
            "versions": list(e.versions),
            "default_version": e.versions[0],
            "port": e.port,
            "env_var": e.env_var,
            "can_restore": e.can_restore,
        }
        for e in ENGINES.values()
    ]
