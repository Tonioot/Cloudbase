"""The primary's side of the node network: the hub interface, the list of nodes and their health."""
import asyncio
import logging
import time
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core import config as _cfg
from core.database import AsyncSessionLocal
from core.models import Node
from mesh import addressing, helper

log = logging.getLogger("cloudbase.mesh")

# A node sends a keepalive every 25 s; three missed is a broken link.
HANDSHAKE_TIMEOUT = 180

_lock = asyncio.Lock()
_hub = {"up": False, "public_key": None, "error": None}


def enabled() -> bool:
    return bool(_cfg.get_wireguard().get("enabled"))


def info() -> dict:
    cfg = _cfg.get_wireguard()
    return {"enabled": enabled(), "up": _hub["up"], "error": _hub["error"], "port": int(cfg["port"]),
            "subnet": cfg["subnet"], "endpoint": cfg.get("endpoint") or "", "public_key": _hub["public_key"]}


def state_from_handshake(handshake_at: int, now: float) -> str:
    """'up' while the node has shaken hands recently."""
    return "up" if handshake_at and now - handshake_at <= HANDSHAKE_TIMEOUT else "down"


async def enrolled_nodes(db: AsyncSession) -> list[Node]:
    result = await db.execute(select(Node).where(Node.wg_public_key.is_not(None), Node.is_local == False))  # noqa: E712
    return list(result.scalars().all())


async def ensure_hub() -> bool:
    """Bring the hub interface up (once). Returns whether it is up."""
    if not enabled():
        return False
    async with _lock:
        if not _hub["up"]:
            cfg = _cfg.get_wireguard()
            try:
                _hub["public_key"] = await asyncio.to_thread(
                    helper.start_hub, addressing.hub_interface(cfg["subnet"]), int(cfg["port"]))
                _hub["up"], _hub["error"] = True, None
                log.info("[mesh] hub up on %s port %s", cfg["subnet"], cfg["port"])
            except (helper.HelperError, ValueError) as exc:
                _hub["up"], _hub["error"] = False, str(exc)
                log.warning("[mesh] hub could not start: %s", exc)
                return False
    await sync_peers()
    return _hub["up"]


async def sync_peers() -> None:
    """Tell the hub which nodes may connect."""
    if not _hub["up"]:
        return
    async with AsyncSessionLocal() as db:
        peers = [{"public_key": n.wg_public_key, "ip": n.wg_ip} for n in await enrolled_nodes(db) if n.wg_ip]
    try:
        await asyncio.to_thread(helper.set_peers, peers)
    except helper.HelperError as exc:
        _hub["up"], _hub["error"] = False, str(exc)       # the next tick starts again from the top
        log.warning("[mesh] could not update peers: %s", exc)


async def enroll(db: AsyncSession, node: Node, public_key: str) -> dict:
    """A node announces its key; it gets an address and the hub learns about it."""
    if not enabled():
        raise HTTPException(409, "The node network is turned off on the primary")
    if not addressing.is_valid_key(public_key):
        raise HTTPException(400, "Invalid WireGuard public key")
    cfg = _cfg.get_wireguard()
    others = (await db.execute(select(Node).where(Node.id != node.id))).scalars().all()
    if any(o.wg_public_key == public_key for o in others):
        raise HTTPException(409, "That key already belongs to another node")
    if not node.wg_ip:
        try:
            node.wg_ip = addressing.next_free_ip(cfg["subnet"], [o.wg_ip for o in others])
        except ValueError as exc:
            raise HTTPException(409, str(exc))
    node.wg_public_key = public_key
    await db.commit()
    if not await ensure_hub():
        raise HTTPException(503, f"The hub could not start: {_hub['error'] or 'unknown error'}")
    return {"address": f"{node.wg_ip}/32", "allowed": str(addressing.network(cfg["subnet"])),
            "hub_public_key": _hub["public_key"], "port": int(cfg["port"]), "endpoint": cfg.get("endpoint") or ""}


async def refresh_states() -> list[tuple[int, str | None, str]]:
    """Read the handshakes and store each node's state. Returns [(node_id, old, new)] for the changes."""
    try:
        peers = {p["public_key"]: p["latest_handshake"] for p in (await asyncio.to_thread(helper.status))["peers"]}
    except helper.HelperError as exc:
        peers = None
        _hub["up"], _hub["error"] = False, str(exc)
    now = time.time()
    changes = []
    async with AsyncSessionLocal() as db:
        for node in await enrolled_nodes(db):
            seen = (peers or {}).get(node.wg_public_key, 0)
            new = state_from_handshake(seen, now)
            if seen:
                node.wg_handshake_at = datetime.utcfromtimestamp(seen)
            if node.wg_state != new:
                changes.append((node.id, node.wg_state, new))
                node.wg_state = new
        await db.commit()
    return changes
