"""Primary-side cache of database links: what each app gets from its linked databases.

Container starts happen in many places (start, restart, scale, deploys, recovery)
and all of them build their Docker options through _docker_runtime_options(). That
function is synchronous, so the links are kept in memory and refreshed whenever a
database or link changes.

Each link carries both a direct URL (for instances on the database's node) and a
remote URL over the node network (for instances elsewhere); the node that starts the
container picks one, see db_proxy.resolve_links().
"""

import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dbs import engines as db_engines
from dbs import docker as dbs_docker
from dbs import proxy as db_proxy
from core import config as _cfg
from core.env_crypto import decrypt_text
from mesh import addressing
from core.models import Database, DatabaseLink, Node

log = logging.getLogger("cloudbase.databases")

# app_id → linked database entries for docker_options["linked_databases"]
_app_links: dict[int, list[dict]] = {}


def connection_url(database, credentials: dict) -> str:
    """The direct URL, as used by apps on the database's own node."""
    engine = db_engines.get_engine(database.engine)
    return engine.url(credentials, dbs_docker.container_name(database.id), engine.port)


def node_address(node) -> str | None:
    """Where a node's databases are reached from the other nodes: its address on the node network."""
    if node is None:
        return None
    if node.is_local:
        cfg = _cfg.get_wireguard()
        return addressing.hub_ip(cfg["subnet"]) if cfg.get("enabled") else None
    return node.wg_ip or None


def _remote_url(engine, creds: dict, database_id: int, node) -> str | None:
    address = node_address(node)
    return engine.url(creds, address, dbs_docker.published_port(database_id)) if address else None


def decrypt_credentials(database) -> dict:
    return json.loads(decrypt_text(database.credentials))


async def refresh(db: AsyncSession) -> None:

    local = (await db.execute(select(Node.id).where(Node.is_local == True))).scalar_one_or_none()
    db_proxy.set_local_node_id(local)

    nodes = {n.id: n for n in (await db.execute(select(Node))).scalars()}
    result = await db.execute(
        select(DatabaseLink, Database).join(Database, Database.id == DatabaseLink.database_id)
    )
    links: dict[int, list[dict]] = {}
    for link, database in result.all():
        try:
            engine = db_engines.get_engine(database.engine)
            creds = decrypt_credentials(database)
            host = dbs_docker.container_name(database.id)
            links.setdefault(link.app_id, []).append({
                "database_id": database.id,
                "node_id": database.node_id,
                "env_var": link.env_var_name,
                "host": host,
                "direct_url": engine.url(creds, host, engine.port),
                "remote_url": _remote_url(engine, creds, database.id, nodes.get(database.node_id)),
            })
        except Exception:
            log.warning("[db-links] cannot build URLs for database %s", database.id, exc_info=True)
    _app_links.clear()
    _app_links.update(links)


def runtime_options(app_id: int) -> dict:
    """Extra Docker options for an app container: its database links."""
    links = _app_links.get(app_id)
    if not links:
        return {}
    return {"linked_databases": [dict(l) for l in links]}
