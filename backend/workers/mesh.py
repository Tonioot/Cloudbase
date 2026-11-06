"""Keeps the node network healthy: starts the hub, follows the handshakes, reacts to broken links."""
import asyncio
import logging

from core.database import AsyncSessionLocal
from mesh import hub
from nodes import promote

log = logging.getLogger("cloudbase.mesh")
INTERVAL = 10


async def _has_enrolled_nodes() -> bool:
    async with AsyncSessionLocal() as db:
        return bool(await hub.enrolled_nodes(db))


async def tick() -> None:
    if not hub.enabled() or not await _has_enrolled_nodes():
        return
    if not hub.info()["up"]:
        await hub.ensure_hub()
    for node_id, old, new in await hub.refresh_states():
        log.info("[mesh] node %d: %s -> %s", node_id, old, new)
        if new == "down":
            await promote.link_down(node_id)


async def _mesh_monitor() -> None:
    await asyncio.sleep(5)
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.warning("[mesh] monitor error: %s", exc)
        await asyncio.sleep(INTERVAL)
