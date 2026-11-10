from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
import os

DATA_DIR = os.path.expanduser("~/.cloudbase")
os.makedirs(DATA_DIR, exist_ok=True)

DATABASE_URL = f"sqlite+aiosqlite:///{DATA_DIR}/cloudbase.db"

from sqlalchemy import event as _sa_event
from sqlalchemy.pool import NullPool

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    poolclass=NullPool,
    connect_args={"timeout": 30},
)

@_sa_event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragmas(conn, _):
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


# Instances from before the primary was a node row of its own belong to it
REPLICAS_WITHOUT_NODE_SQL = (
    "UPDATE application_replicas SET node_id = (SELECT id FROM nodes WHERE is_local = 1 LIMIT 1) "
    "WHERE node_id IS NULL AND EXISTS (SELECT 1 FROM nodes WHERE is_local = 1)"
)

# Statuses older versions wrote that nothing understands any more: the instance is on its way up
OLD_INSTANCE_STATUSES_SQL = "UPDATE application_replicas SET status = 'starting' WHERE status IN ('restarting', 'deploying')"


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


async def init_db():
    import datetime as _dt
    # Importing every model registers its table with Base.metadata for create_all below.
    from core.models import Application, Node, NodeInvite, AuditLog, StatsHistory, User, SystemConfig, Role, Permission, role_permissions  # noqa: F401
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Migrate existing DBs: add columns introduced after initial schema.
        result = await conn.exec_driver_sql("PRAGMA table_info(applications)")
        existing_columns = {row[1] for row in result.fetchall()}

        for col, definition in [
            ("restart_policy",    "VARCHAR(20) NOT NULL DEFAULT 'no'"),
            ("docker_cpu_limit",  "FLOAT"),
            ("docker_memory_limit_mb", "INTEGER"),
            ("docker_read_only_root", "BOOLEAN NOT NULL DEFAULT 0"),
            ("docker_tmpfs_enabled", "BOOLEAN NOT NULL DEFAULT 0"),
            ("docker_tmpfs_size_mb", "INTEGER"),
            ("maintenance_mode",  "BOOLEAN NOT NULL DEFAULT 0"),
            ("update_mode",       "BOOLEAN NOT NULL DEFAULT 0"),
            ("downtime_page",     "TEXT"),
            ("update_page",       "TEXT"),
            ("restart_page",      "TEXT"),
            ("starting_page",     "TEXT"),
            ("extra_domains",     "TEXT"),
            ("redirect_domains",  "TEXT"),
            ("node_id",           "INTEGER"),
            ("last_error",        "TEXT"),
            ("source_revision",   "VARCHAR(120)"),
            ("image_revision",    "VARCHAR(120)"),
            ("no_web",            "BOOLEAN NOT NULL DEFAULT 0"),
            ("build_command",     "VARCHAR(500)"),
            ("health_path",            "VARCHAR(200) NOT NULL DEFAULT '/'"),
            ("health_expect",          "VARCHAR(60) NOT NULL DEFAULT ''"),
            ("health_timeout_seconds", "INTEGER NOT NULL DEFAULT 600"),
            ("autoscale_enabled",      "BOOLEAN NOT NULL DEFAULT 0"),
            ("autoscale_min_replicas", "INTEGER NOT NULL DEFAULT 1"),
            ("autoscale_max_replicas", "INTEGER NOT NULL DEFAULT 4"),
            ("autoscale_cpu_target",   "FLOAT NOT NULL DEFAULT 70.0"),
            ("auto_deploy",            "BOOLEAN NOT NULL DEFAULT 0"),
            ("deploy_branch",          "VARCHAR(200)"),
            ("deploy_strategy",        "VARCHAR(20) NOT NULL DEFAULT 'rolling'"),
            ("auto_deploy_interval",   "INTEGER NOT NULL DEFAULT 60"),
            ("notifications_muted_until", "DATETIME"),
            ("cron_schedule",          "VARCHAR(120)"),
            ("cron_timezone",          "VARCHAR(64)"),
            ("cron_enabled",           "BOOLEAN NOT NULL DEFAULT 1"),
            ("cron_timeout_seconds",   "INTEGER NOT NULL DEFAULT 3600"),
            ("cron_last_slot",         "DATETIME"),
        ]:
            if col in existing_columns:
                continue
            await conn.exec_driver_sql(
                f"ALTER TABLE applications ADD COLUMN {col} {definition}"
            )

        result = await conn.exec_driver_sql("PRAGMA table_info(nodes)")
        existing_node_cols = {row[1] for row in result.fetchall()}
        for col, definition in [
            ("connection_type",     "VARCHAR(20)"),
            ("agent_version",       "VARCHAR(40)"),
            ("node_cpu_percent",    "FLOAT"),
            ("node_memory_percent", "FLOAT"),
            ("node_disk_percent",   "FLOAT"),
            ("wg_public_key",       "VARCHAR(60)"),
            ("wg_ip",               "VARCHAR(20)"),
            ("wg_state",            "VARCHAR(20)"),
            ("wg_handshake_at",     "DATETIME"),
            ("node_net_rx",         "FLOAT"),
            ("node_net_tx",         "FLOAT"),
            ("node_disk_read",      "FLOAT"),
            ("node_disk_write",     "FLOAT"),
            ("node_load_1",         "FLOAT"),
        ]:
            if col in existing_node_cols:
                continue
            await conn.exec_driver_sql(
                f"ALTER TABLE nodes ADD COLUMN {col} {definition}"
            )

        # Commands to nodes live in memory now (nodes/commands.py); the old table's foreign keys would block deletes
        await conn.exec_driver_sql("DROP TABLE IF EXISTS node_commands")

        result = await conn.exec_driver_sql("PRAGMA table_info(databases)")
        existing_db_cols = {row[1] for row in result.fetchall()}
        for col, definition in [
            ("public_enabled",   "BOOLEAN NOT NULL DEFAULT 0"),
            ("public_allowlist", "TEXT"),
            ("public_tls",       "BOOLEAN NOT NULL DEFAULT 1"),
            ("backup_schedule",  "VARCHAR(10) NOT NULL DEFAULT 'daily'"),
            ("backup_hour",      "INTEGER NOT NULL DEFAULT 3"),
            ("backup_retention", "INTEGER NOT NULL DEFAULT 7"),
        ]:
            if col in existing_db_cols:
                continue
            await conn.exec_driver_sql(
                f"ALTER TABLE databases ADD COLUMN {col} {definition}"
            )

        result = await conn.exec_driver_sql("PRAGMA table_info(database_backups)")
        existing_backup_cols = {row[1] for row in result.fetchall()}
        for col, definition in [
            ("offsite_status", "VARCHAR(10)"),
            ("offsite_key",    "VARCHAR(500)"),
            ("offsite_error",  "TEXT"),
            ("offsite_at",     "DATETIME"),
        ]:
            if col in existing_backup_cols:
                continue
            await conn.exec_driver_sql(
                f"ALTER TABLE database_backups ADD COLUMN {col} {definition}"
            )

        # nodes — index auth_token for fast per-request token lookups
        await conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_nodes_auth_token ON nodes (auth_token)"
        )

        # audit_logs — new table, created by create_all above; ensure composite index
        await conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_audit_logs_app_ts "
            "ON audit_logs (app_id, timestamp)"
        )

        # stats_history — new table; ensure composite index and purge old rows on startup
        await conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_stats_history_app_ts "
            "ON stats_history (app_id, timestamp)"
        )
        cutoff = (_dt.datetime.utcnow() - _dt.timedelta(days=7)).isoformat()
        await conn.exec_driver_sql(
            f"DELETE FROM stats_history WHERE timestamp < '{cutoff}'"
        )

        # stats_history column migrations (added after initial schema)
        result = await conn.exec_driver_sql("PRAGMA table_info(stats_history)")
        existing_sh_cols = {row[1] for row in result.fetchall()}
        for col, definition in [
            ("net_mb",  "FLOAT"),
            ("disk_mb", "FLOAT"),
        ]:
            if col in existing_sh_cols:
                continue
            await conn.exec_driver_sql(
                f"ALTER TABLE stats_history ADD COLUMN {col} {definition}"
            )

        result = await conn.exec_driver_sql("PRAGMA table_info(node_metrics_history)")
        existing_history_cols = {row[1] for row in result.fetchall()}
        for col in ("net_rx_bytes", "net_tx_bytes", "disk_read_bytes", "disk_write_bytes", "load_1"):
            if existing_history_cols and col not in existing_history_cols:
                await conn.exec_driver_sql(f"ALTER TABLE node_metrics_history ADD COLUMN {col} FLOAT")

        # application_replicas
        result = await conn.exec_driver_sql("PRAGMA table_info(application_replicas)")
        existing_replica_cols = {row[1] for row in result.fetchall()}
        for col, definition in [
            ("docker_cpu_limit",         "FLOAT"),
            ("docker_memory_limit_mb",   "INTEGER"),
            ("docker_read_only_root",    "BOOLEAN NOT NULL DEFAULT 0"),
            ("docker_tmpfs_enabled",     "BOOLEAN NOT NULL DEFAULT 0"),
            ("docker_tmpfs_size_mb",     "INTEGER"),
            ("substatus",               "VARCHAR(40)"),
            ("desired",                 "VARCHAR(10)"),
            ("spec_json",               "TEXT"),
            ("spec_hash",               "VARCHAR(20)"),
        ]:
            if col in existing_replica_cols:
                continue
            await conn.exec_driver_sql(
                f"ALTER TABLE application_replicas ADD COLUMN {col} {definition}"
            )
        await conn.exec_driver_sql(REPLICAS_WITHOUT_NODE_SQL)
        await conn.exec_driver_sql(OLD_INSTANCE_STATUSES_SQL)
        # Instances on other nodes written before nodes were driven by desired state: what they were doing
        # is what they should keep doing (nodes/desired.py writes their spec the first time a node asks)
        await conn.exec_driver_sql(
            "UPDATE application_replicas SET desired = CASE WHEN status IN "
            "('running','starting','restarting','node_offline','pending') THEN 'running' ELSE 'stopped' END "
            "WHERE desired IS NULL AND node_id IN (SELECT id FROM nodes WHERE is_local = 0)"
        )

        # users table — add role_id column if missing
        result = await conn.exec_driver_sql("PRAGMA table_info(users)")
        existing_user_cols = {row[1] for row in result.fetchall()}
        if "role_id" not in existing_user_cols:
            await conn.exec_driver_sql("ALTER TABLE users ADD COLUMN role_id INTEGER REFERENCES roles(id) ON DELETE SET NULL")
        if "session_version" not in existing_user_cols:
            await conn.exec_driver_sql("ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0")
        if "notifications_read_at" not in existing_user_cols:
            await conn.exec_driver_sql("ALTER TABLE users ADD COLUMN notifications_read_at DATETIME")

        result = await conn.exec_driver_sql("PRAGMA table_info(notifications)")
        existing_notif_cols = {row[1] for row in result.fetchall()}
        for col, definition in [
            ("in_inbox",    "BOOLEAN NOT NULL DEFAULT 1"),
            ("reminded_at", "DATETIME"),
        ]:
            if col not in existing_notif_cols:
                await conn.exec_driver_sql(f"ALTER TABLE notifications ADD COLUMN {col} {definition}")

        # notifications — find the open problem for a dedupe key quickly
        await conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_notifications_key_state "
            "ON notifications (dedupe_key, state)"
        )

        # applications table — add github_token_id column if missing
        result = await conn.exec_driver_sql("PRAGMA table_info(applications)")
        existing_app_cols = {row[1] for row in result.fetchall()}
        if "github_token_id" not in existing_app_cols:
            await conn.exec_driver_sql("ALTER TABLE applications ADD COLUMN github_token_id VARCHAR(100)")

    # Seed default permissions, roles, and admin user
    import os as _os
    from sqlalchemy import select as _select
    _CREDENTIALS_FILE = _os.path.expanduser("~/.cloudbase/credentials")

    # All known permissions in the system
    ALL_PERMISSIONS = [
        # Apps — viewing (includes logs and stats)
        ("apps.view",         "View applications, status, logs and statistics"),
        # Apps — lifecycle
        ("apps.deploy",       "Deploy (create) new applications"),
        ("apps.start",        "Start applications"),
        ("apps.stop",         "Stop applications"),
        ("apps.restart",      "Restart applications"),
        ("apps.pull",         "Pull latest code and rebuild Docker image"),
        ("apps.scale",        "Add or remove application instances"),
        # Apps — configuration & removal
        ("apps.configure",    "Edit application settings, nginx, certs, env vars and maintenance pages"),
        ("apps.delete",       "Delete applications"),
        # Nodes
        ("nodes.view",        "View nodes and their status"),
        ("nodes.add",         "Add new nodes and create node invites"),
        ("nodes.configure",   "Rename, enable and disable nodes"),
        ("nodes.delete",      "Delete nodes"),
        # Databases
        ("databases.view",        "View databases, their status, logs and statistics"),
        ("databases.create",      "Create new databases"),
        ("databases.manage",      "Start, stop and restart databases, change limits and link them to apps"),
        ("databases.credentials", "View database passwords and connection strings"),
        ("databases.backup",      "Create, download, upload and restore database backups"),
        ("databases.delete",      "Delete databases"),
        # Observability
        ("audit.view",        "View audit logs"),
        ("notifications.view",   "See notifications in the panel"),
        ("notifications.manage", "Manage notification channels and which events they receive"),
        # System & users
        ("system.manage",     "Manage Cloudbase server settings, server logs and nginx"),
        ("users.manage",      "Create, edit and delete users"),
        ("roles.manage",      "Create, edit and delete roles"),
        ("tokens.manage",     "Manage Git tokens (access tokens for private repositories)"),
        ("secrets.export",    "Download environment variables and Git tokens in clear text (app export). Not part of the Administrator role: grant it on purpose"),
    ]

    # Rename stale permission names that may already exist in the database
    _RENAMES = [
        ("github.manage", "tokens.manage"),
    ]

    # Permissions that no longer exist — clean them up if they're still in the DB
    # Old coarse-grained permissions replaced by the granular set above.
    _REMOVED_PERMISSIONS = [
        "system.view",
        "apps.create",   # replaced by apps.deploy / apps.start / apps.stop / apps.restart / apps.pull / apps.scale
        "apps.manage",   # replaced by apps.configure / apps.delete
        "nodes.manage",  # replaced by nodes.configure / nodes.delete
        "logs.view",     # merged into apps.view
        "stats.view",    # merged into apps.view
    ]

    async with AsyncSessionLocal() as session:
        # Apply renames before seeding so existing DBs stay consistent
        for old_name, new_name in _RENAMES:
            res = await session.execute(_select(Permission).where(Permission.name == old_name))
            old_perm = res.scalar_one_or_none()
            if old_perm is not None:
                res2 = await session.execute(_select(Permission).where(Permission.name == new_name))
                if res2.scalar_one_or_none() is None:
                    old_perm.name = new_name
        await session.commit()

        # Drop removed permissions — cascades remove their role assignments
        for dead_name in _REMOVED_PERMISSIONS:
            res = await session.execute(_select(Permission).where(Permission.name == dead_name))
            dead = res.scalar_one_or_none()
            if dead is not None:
                await session.delete(dead)
        await session.commit()

        # Remove non-built-in seeded roles that were added in earlier versions
        _REMOVED_ROLES = ["Developer"]
        for dead_role in _REMOVED_ROLES:
            res = await session.execute(_select(Role).where(Role.name == dead_role))
            role_obj = res.scalar_one_or_none()
            if role_obj is not None:
                # Unassign users from this role before deleting
                from core.models import User as _User
                await session.execute(
                    _User.__table__.update()
                    .where(_User.__table__.c.role_id == role_obj.id)
                    .values(role_id=None)
                )
                await session.delete(role_obj)
        await session.commit()

        # Ensure all permissions exist
        perm_map: dict[str, int] = {}
        for pname, pdesc in ALL_PERMISSIONS:
            res = await session.execute(_select(Permission).where(Permission.name == pname))
            perm = res.scalar_one_or_none()
            if perm is None:
                perm = Permission(name=pname, description=pdesc)
                session.add(perm)
                await session.flush()
            else:
                perm.description = pdesc  # keep descriptions up-to-date
            perm_map[pname] = perm.id

        # Seed built-in roles: "Viewer" (read-only) and "Administrator" (full access).
        # Built-in roles are reset to their canonical permission set on every startup
        # so they cannot drift from the source of truth.
        from sqlalchemy import delete as _delete
        viewer_perms = ["apps.view", "nodes.view", "databases.view", "audit.view", "notifications.view"]
        # Permissions that reveal secrets are never part of a built-in role. Only the root account
        # (which bypasses roles) has them, and a custom role gets them when someone chooses to.
        OPT_IN_PERMISSIONS = {"secrets.export"}
        admin_perms  = [p for p, _ in ALL_PERMISSIONS if p not in OPT_IN_PERMISSIONS]

        for role_name, role_desc, perms in [
            ("Viewer",        "Read-only access to all resources", viewer_perms),
            ("Administrator", "Full access to all features",       admin_perms),
        ]:
            res = await session.execute(_select(Role).where(Role.name == role_name))
            role_obj = res.scalar_one_or_none()
            if role_obj is None:
                role_obj = Role(name=role_name, description=role_desc)
                session.add(role_obj)
                await session.flush()
            else:
                role_obj.description = role_desc  # keep description up-to-date
            # Re-sync permissions to canonical set
            await session.execute(_delete(role_permissions).where(role_permissions.c.role_id == role_obj.id))
            for pname in perms:
                pid = perm_map.get(pname)
                if pid:
                    await session.execute(
                        role_permissions.insert().values(role_id=role_obj.id, permission_id=pid)
                    )

        await session.commit()

        # First run: seed the root user from the credentials file written at startup
        existing = await session.execute(_select(User).limit(1))
        if existing.scalar_one_or_none() is None and _os.path.exists(_CREDENTIALS_FILE):
            with open(_CREDENTIALS_FILE) as _f:
                hashed = _f.read().strip()
            if hashed:
                session.add(User(
                    username="admin",
                    password_hash=hashed,
                    role="Root",
                    role_id=None,  # Root has no role — full access is inherent
                ))
                await session.commit()

        # Always strip any role assignment from the root account — it always has full access
        res = await session.execute(_select(User).where(User.username == "admin"))
        root_user = res.scalar_one_or_none()
        if root_user is not None:
            changed = False
            if root_user.role_id is not None:
                root_user.role_id = None
                changed = True
            if root_user.role != "Root":
                root_user.role = "Root"
                changed = True
            if changed:
                await session.commit()
