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

    @property
    def can_restore(self) -> bool:
        return self.restore is not None or self.restore_via_helper is not None

    def image_ref(self, version: str) -> str:
        return f"{self.image}:{version}"


# ── Tuning helpers ────────────────────────────────────────────────────────────

def _postgres_command(_creds: dict, mem_mb: Optional[int]) -> Optional[list[str]]:
    if not mem_mb:
        return None
    return [
        "postgres",
        "-c", f"shared_buffers={max(mem_mb // 4, 32)}MB",
        "-c", f"effective_cache_size={max(mem_mb * 3 // 4, 64)}MB",
    ]


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

def _pg_restore(ctx: dict) -> str:
    if ctx["pg_custom"]:
        return (f'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists '
                f'--no-owner --no-privileges {ctx["path"]}')
    return f'psql -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f {ctx["path"]}'


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
        container_env=lambda c: {
            "POSTGRES_USER": c["user"],
            "POSTGRES_PASSWORD": c["password"],
            "POSTGRES_DB": c["db_name"],
        },
        command=_postgres_command,
        healthcheck='pg_isready -q -U "$POSTGRES_USER" -d "$POSTGRES_DB"',
        data_dir=_postgres_data_dir,
        url=lambda c, host, port: f"postgresql://{c['user']}:{c['password']}@{host}:{port}/{c['db_name']}",
        backup={"ext": "dump", "gzip": False,
                "dump": 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --no-owner'},
        restore=_pg_restore,
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
        # Self-signed by the Cloudbase CA: encrypt without verifying, or use tlsCAFile.
        tls_url=lambda u: _with_param(u, "tls=true&tlsAllowInvalidCertificates=true"),
        tls_verify_url=lambda u: _with_param(u, "tls=true&tlsCAFile=cloudbase-database-ca.crt"),
    ),
}

# Names that collide with built-in users or databases of one of the engines.
RESERVED_NAMES = {
    "root", "admin", "mysql", "mariadb", "postgres", "template0", "template1",
    "sys", "information-schema", "performance-schema", "local", "config", "test", "default",
}


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
