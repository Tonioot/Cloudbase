"""Primary-side cache of database links: which env vars each app gets from its
linked databases.

Container starts happen in many places (start, restart, scale, deploys, recovery)
and all of them build their Docker options through _docker_runtime_options(). That
function is synchronous, so the links are kept in memory and refreshed whenever a
database or link changes.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import db_engines
import db_manager
from env_crypto import decrypt_text
import json

log = logging.getLogger("cloudbase.databases")

# app_id → {env var name: connection URL}
_app_env: dict[int, dict[str, str]] = {}


def connection_url(database, credentials: dict) -> str:
    engine = db_engines.get_engine(database.engine)
    return engine.url(credentials, db_manager.container_name(database.id))


def decrypt_credentials(database) -> dict:
    return json.loads(decrypt_text(database.credentials))


async def refresh(db: AsyncSession) -> None:
    from models import Database, DatabaseLink

    result = await db.execute(
        select(DatabaseLink, Database).join(Database, Database.id == DatabaseLink.database_id)
    )
    env: dict[int, dict[str, str]] = {}
    for link, database in result.all():
        try:
            url = connection_url(database, decrypt_credentials(database))
        except Exception:
            log.warning("[db-links] cannot build URL for database %s", database.id, exc_info=True)
            continue
        env.setdefault(link.app_id, {})[link.env_var_name] = url
    _app_env.clear()
    _app_env.update(env)


def runtime_options(app_id: int) -> dict:
    """Extra Docker options for an app container: the database network and URLs."""
    env = _app_env.get(app_id)
    if not env:
        return {}
    return {"network": db_manager.NETWORK, "linked_env": dict(env)}
