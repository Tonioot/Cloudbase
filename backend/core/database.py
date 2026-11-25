import logging
import os

from sqlalchemy import delete, event, select, update
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

log = logging.getLogger("cloudbase.database")

DATA_DIR = os.path.expanduser("~/.cloudbase")
os.makedirs(DATA_DIR, exist_ok=True)

DATABASE_URL = f"sqlite+aiosqlite:///{DATA_DIR}/cloudbase.db"
_CREDENTIALS_FILE = os.path.join(DATA_DIR, "credentials")

# A small pool of connections: opening one costs a thread and three PRAGMAs (about 2 ms), a pooled one about 0.4 ms.
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_size=5,
    max_overflow=10,
    connect_args={"timeout": 30},
)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragmas(conn, _):
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


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
    # Projects
    ("projects.manage",   "Create, rename and delete projects, move apps and databases between them and change their shared settings"),
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
    ("system.platform",   "Set the panel, app and database domains and the brand of the pages visitors see: what a platform built on Cloudbase sets up itself"),
    ("tokens.manage",     "Manage Git tokens (access tokens for private repositories)"),
    ("secrets.export",    "Download environment variables and Git tokens in clear text (app export). Not part of the Administrator role: grant it on purpose"),
]

VIEWER_PERMISSIONS = ["apps.view", "nodes.view", "databases.view", "audit.view", "notifications.view"]
# Permissions that reveal secrets are never part of a built-in role. Only the root account
# (which bypasses roles) has them, and a custom role gets them when someone chooses to.
OPT_IN_PERMISSIONS = {"secrets.export"}


def _add_new_columns(conn) -> None:
    """A column added to a model is added to its existing table (create_all only makes missing tables), with its
    default for the rows already there. Nothing is ever changed or removed, so this needs no versions."""
    from sqlalchemy import inspect
    existing = inspect(conn)
    for table in Base.metadata.sorted_tables:
        if not existing.has_table(table.name):
            continue
        have = {c["name"] for c in existing.get_columns(table.name)}
        for column in table.columns:
            if column.name in have:
                continue
            default = column.default.arg if column.default is not None and column.default.is_scalar else None
            if not column.nullable and default is None:
                raise RuntimeError(f"New column {table.name}.{column.name} needs a default or nullable=True")
            ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column.type.compile(conn.dialect)}'
            if default is not None:
                ddl += f" DEFAULT {_sql_literal(default)}"
            conn.exec_driver_sql(ddl)
            log.info("Added column %s.%s", table.name, column.name)


def _sql_literal(value) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def _users_lack_project_access(conn) -> bool:
    from sqlalchemy import inspect
    existing = inspect(conn)
    return existing.has_table("users") and "project_access" not in {c["name"] for c in existing.get_columns("users")}


def _create_and_upgrade_tables(conn) -> None:
    """Make the missing tables and columns. Where a new column cannot just take a default, the rows already there
    get what their old behaviour was."""
    had_no_project_access = _users_lack_project_access(conn)
    Base.metadata.create_all(conn)
    _add_new_columns(conn)
    if had_no_project_access:
        # Until now being a member of a project limited a user to their projects: keep exactly that, once
        conn.exec_driver_sql("UPDATE users SET project_access = 'selected' WHERE id IN (SELECT user_id FROM project_members)")


async def init_db():
    """Create the tables that are missing, and make sure the permissions, the built-in roles and the root account exist."""
    # Importing the models registers their tables with Base.metadata (they import Base from here, so not at the top)
    from core.models import Application, Database, Permission, Project, Role, User, role_permissions
    async with engine.begin() as conn:
        await conn.run_sync(_create_and_upgrade_tables)

    async with AsyncSessionLocal() as session:
        perm_map: dict[str, int] = {}
        for pname, pdesc in ALL_PERMISSIONS:
            perm = (await session.execute(select(Permission).where(Permission.name == pname))).scalar_one_or_none()
            if perm is None:
                perm = Permission(name=pname, description=pdesc)
                session.add(perm)
                await session.flush()
            else:
                perm.description = pdesc  # keep descriptions up-to-date
            perm_map[pname] = perm.id
        known = set(perm_map)
        for stale in (await session.execute(select(Permission).where(Permission.name.notin_(known)))).scalars().all():
            await session.execute(delete(role_permissions).where(role_permissions.c.permission_id == stale.id))
            await session.delete(stale)           # a permission nothing checks any more (users.manage, roles.manage)

        # Built-in roles are reset to their canonical permission set on every startup so they cannot drift
        admin_perms = [p for p, _ in ALL_PERMISSIONS if p not in OPT_IN_PERMISSIONS]
        for role_name, role_desc, perms in [
            ("Viewer",        "Read-only access to all resources", VIEWER_PERMISSIONS),
            ("Administrator", "Full access to all features",       admin_perms),
        ]:
            role = (await session.execute(select(Role).where(Role.name == role_name))).scalar_one_or_none()
            if role is None:
                role = Role(name=role_name, description=role_desc)
                session.add(role)
                await session.flush()
            else:
                role.description = role_desc
            await session.execute(delete(role_permissions).where(role_permissions.c.role_id == role.id))
            for pname in perms:
                await session.execute(role_permissions.insert().values(role_id=role.id, permission_id=perm_map[pname]))
        await session.commit()

        # First run: the root user, from the credentials file written at startup
        if (await session.execute(select(User).limit(1))).scalar_one_or_none() is None and os.path.exists(_CREDENTIALS_FILE):
            with open(_CREDENTIALS_FILE) as f:
                hashed = f.read().strip()
            if hashed:
                session.add(User(username="admin", password_hash=hashed, role="Root", role_id=None))  # Root needs no role
                await session.commit()

        # Every app and database belongs to a project; the first one holds what existed before projects
        default = (await session.execute(select(Project).where(Project.is_default == True))).scalar_one_or_none()  # noqa: E712
        if default is None:
            # Holds what belongs to no project; the panel shows it as "No project", not as a project of its own
            default = Project(name="No project", is_default=True)
            session.add(default)
            await session.flush()
        for model in (Application, Database):
            await session.execute(update(model).where(model.project_id.is_(None)).values(project_id=default.id))
        await session.commit()

        # The root account never has a role: it always has full access
        root = (await session.execute(select(User).where(User.username == "admin"))).scalar_one_or_none()
        if root is not None and (root.role_id is not None or root.role != "Root"):
            root.role_id, root.role = None, "Root"
            await session.commit()
