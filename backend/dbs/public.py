"""Public database access on the primary: which ports are open (sync_public_access), and the way to a database
behind one (open_db_stream): the container here, or its node over the node network. The listeners are in dbs/proxy.py."""
import asyncio
from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from core import config as _cfg
from core import system_config as _syscfg
from dbs import proxy as db_proxy
from dbs import public_certs
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


def public_host_for(database: Database) -> str:
    """The address a database is reached at from outside: its own hostname when it has one, else
    <name>.<database domain> when that is set in Domain & SSL, else the primary's IP. The names have to point at the
    primary itself (DNS only), as a web proxy such as Cloudflare does not carry database traffic."""
    if database.public_hostname:
        return database.public_hostname
    domain = _syscfg.get_db_domain_cached()
    return f"{database.name}.{domain}" if domain else _public_host()


def _certificate_hosts() -> list[str]:
    """The names of Cloudbase's own certificate: only used for a database on the bare IP address. A database reached
    by a name gets a real certificate (see dbs/public_certs.py)."""
    return sorted(n for n in {_public_ip, "localhost"} if n)


def pick_public_port(database: Database, taken: set[int]) -> int | None:
    """The port a database keeps for public access: the one it has when that is in the range, else the one the
    old scheme (range start + id) gave it when free, else the lowest free one. None when the range is full."""
    low, high = _cfg.get_port("db_public_min"), _cfg.get_port("db_public_max")
    if database.public_port and low <= database.public_port <= high:
        return database.public_port
    legacy = low + database.id
    if legacy <= high and legacy not in taken:
        return legacy
    return next((p for p in range(low, high + 1) if p not in taken), None)


async def assign_public_port(db: AsyncSession, database: Database) -> bool:
    """Give the database a public port if it has none in the range (it is kept afterwards, also while public access
    is off, so a firewall rule stays valid). Ports of deleted databases are free again. False when none is left."""
    taken = set((await db.execute(
        select(Database.public_port).where(Database.id != database.id, Database.public_port.is_not(None)))).scalars())
    port = pick_public_port(database, taken)
    if port is None:
        return False
    database.public_port = port
    return True


async def sync_public_access() -> None:
    """Open and close the public database ports on the primary."""
    async with AsyncSessionLocal() as db:
        public = (await db.execute(select(Database).where(Database.public_enabled == True))).scalars().all()
        changed = False
        for d in public:
            low, high = _cfg.get_port("db_public_min"), _cfg.get_port("db_public_max")
            if not (d.public_port and low <= d.public_port <= high):
                changed = await assign_public_port(db, d) or changed
        if changed:
            await db.commit()
        public = [d for d in public if d.public_port]
        local = (await db.execute(select(Node).where(Node.is_local == True))).scalar_one_or_none()
    global _public_ip
    _public_ip = local.public_host if local else None
    hosts = _certificate_hosts()
    specs = []
    for d in public:
        state, cert = await public_certs.resolve(d, public_host_for(d), sync_public_access)
        public_certs.STATE[d.id] = state
        if state == "waiting":
            continue                    # no certificate yet: the port stays closed rather than using another one
        specs.append({"database_id": d.id, "port": d.public_port, "allowlist": _allowlist(d), "engine": d.engine,
                      "tls": bool(d.public_tls), "hosts": hosts, "cert": cert})
    for database_id in set(public_certs.STATE) - {d.id for d in public}:
        public_certs.STATE.pop(database_id, None)
    await db_proxy.reconcile_public(specs)


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
    reader, writer = await asyncio.open_connection(address, dbs_lifecycle.published_port(database_id, database.node_port))
    return db_proxy.TcpStream(reader, writer)
