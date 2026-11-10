"""The primary's way to a database (for public access): the container here, or its node over the node network."""
import asyncio
from typing import Optional
from sqlalchemy import select
from dbs import proxy as db_proxy
from dbs import lifecycle as dbs_lifecycle
from dbs.links import node_address
from core.database import AsyncSessionLocal
from core.models import Node, Database
from dbs.ops import _allowlist


# The primary's public IP, refreshed by sync_public_access(). Public database ports are
# always given as this IP: the panel domain may sit behind a proxy such as
# Cloudflare, which only carries web traffic, so database clients would time out.
_public_ip: Optional[str] = None


def _public_host() -> str:
    return _public_ip or ""


async def sync_public_access() -> None:
    """Open and close the public database ports on the primary."""
    async with AsyncSessionLocal() as db:
        public = (await db.execute(select(Database).where(Database.public_enabled == True))).scalars().all()
        local = (await db.execute(select(Node).where(Node.is_local == True))).scalar_one_or_none()
    global _public_ip
    _public_ip = local.public_host if local else None
    hosts = [h for h in (_public_ip, "localhost") if h]
    await db_proxy.reconcile_public([
        {"database_id": d.id, "allowlist": _allowlist(d), "engine": d.engine,
         "tls": bool(d.public_tls), "hosts": hosts}
        for d in public
    ])


async def open_db_stream(database_id: int) -> db_proxy.Stream:
    """A byte stream to a database, wherever it runs."""
    async with AsyncSessionLocal() as db:
        database = await db.get(Database, database_id)
        node = await db.get(Node, database.node_id) if database else None
    if database is None or node is None:
        raise RuntimeError(f"Database {database_id} not found")

    if node.is_local:
        host, port = await asyncio.to_thread(dbs_lifecycle.container_address, database_id)
        reader, writer = await asyncio.open_connection(host, port)
        return db_proxy.TcpStream(reader, writer)

    address = node_address(node)
    if not address:
        raise RuntimeError(f"Node '{node.name}' is not on the node network")
    reader, writer = await asyncio.open_connection(address, dbs_lifecycle.published_port(database_id))
    return db_proxy.TcpStream(reader, writer)
