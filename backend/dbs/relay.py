import asyncio
import json
import secrets
from typing import Optional
from fastapi import WebSocket, HTTPException
from sqlalchemy import select
from dbs import proxy as db_proxy
from dbs import lifecycle as dbs_lifecycle
from core.database import AsyncSessionLocal
from core.models import Node, DatabaseLink, Database, Application, Instance
from nodes.registry import get_node_by_token_or_401
from dbs.ops import _allowlist
from dbs.state import DIAL_TIMEOUT_SECONDS, _control, _pending_dials

_desired_proxies: dict[int, set[int]] = {}  # node id -> database ids it proxies


# The primary's public IP, refreshed by push_proxies(). Public database ports are
# always given as this IP: the panel domain may sit behind a proxy such as
# Cloudflare, which only carries web traffic, so database clients would time out.
_public_ip: Optional[str] = None


def _public_host() -> str:
    return _public_ip or ""


# ── Connections across nodes ──────────────────────────────────────────────────
#
#   app on node X ─► proxy on X ─WS─► primary ─► database on the primary
#                                             └─WS (dial)─► database on node Y
#
# One WebSocket per database connection, binary frames, no multiplexing.

def _desired_proxy_ids(node_id: int) -> set[int]:
    """Database ids a node should proxy (read through this, never import the dict itself: it is rebound)."""
    return _desired_proxies.get(node_id, set())


async def _compute_desired_proxies() -> dict[int, set[int]]:
    """Which databases each node must proxy: those linked to an app with an
    instance on that node, when the database itself lives elsewhere."""
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(DatabaseLink.app_id, Database.id, Database.node_id)
            .join(Database, Database.id == DatabaseLink.database_id)
        )).all()
        if not rows:
            return {}
        app_ids = {r[0] for r in rows}
        instance_rows = (await db.execute(
            select(Instance.app_id, Instance.node_id).where(Instance.app_id.in_(app_ids))
        )).all()
        app_rows = (await db.execute(
            select(Application.id, Application.node_id).where(Application.id.in_(app_ids))
        )).all()
    nodes_by_app: dict[int, set[int]] = {}
    for app_id, node_id in instance_rows:
        if node_id is not None:
            nodes_by_app.setdefault(app_id, set()).add(node_id)
    for app_id, node_id in app_rows:
        if node_id is not None and app_id not in nodes_by_app:
            nodes_by_app[app_id] = {node_id}
    desired: dict[int, set[int]] = {}
    for app_id, db_id, db_node_id in rows:
        for node_id in nodes_by_app.get(app_id, ()):
            if node_id != db_node_id:
                desired.setdefault(node_id, set()).add(db_id)
    return desired


async def _send_control(node_id: int, message: dict) -> bool:
    entry = _control.get(node_id)
    if entry is None:
        return False
    ws, lock = entry
    try:
        async with lock:
            await ws.send_text(json.dumps(message))
        return True
    except Exception:
        return False


async def push_proxies() -> None:
    """Send every node (and the primary itself) the proxies it should run."""
    global _desired_proxies
    _desired_proxies = await _compute_desired_proxies()
    local_id = db_proxy.local_node_id()
    if local_id is not None:
        await db_proxy.reconcile(sorted(_desired_proxies.get(local_id, set())))
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
    for node_id in list(_control):
        await _send_control(node_id, {"type": "proxies", "database_ids": sorted(_desired_proxies.get(node_id, set()))})


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

    token = secrets.token_urlsafe(18)
    future = asyncio.get_running_loop().create_future()
    _pending_dials[token] = (future, node.id)
    try:
        if not await _send_control(node.id, {"type": "dial", "token": token, "database_id": database_id}):
            raise RuntimeError(f"Node '{node.name}' has no database control connection")
        return await asyncio.wait_for(future, timeout=DIAL_TIMEOUT_SECONDS)
    finally:
        _pending_dials.pop(token, None)


async def _authorize_node(websocket: WebSocket) -> Optional[Node]:
    token = websocket.headers.get("x-node-token") or websocket.query_params.get("token")
    if not token:
        await websocket.close(code=1008, reason="Missing node token")
        return None
    async with AsyncSessionLocal() as db:
        try:
            return await get_node_by_token_or_401(token, db)
        except HTTPException:
            await websocket.close(code=1008, reason="Invalid node token")
            return None
