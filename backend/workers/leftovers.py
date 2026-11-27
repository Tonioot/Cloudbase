"""What a deleted app leaves behind: its image and source, on this machine and on every node.

Each machine is told which apps there are, not which one went, so a node that was offline when an app was deleted
catches up the next time (every hour, and right after a delete: workers/log_settings.py). Only ids that were already
given out are looked at (`up_to`; an id is never given out twice, core/models.py), so an app made meanwhile is never
mistaken for a deleted one."""
import asyncio
import logging

from sqlalchemy import func, select

from containers import cleanup as containers_cleanup
from core.database import AsyncSessionLocal
from core.models import Application, Node, SystemConfig
from nodes import commands as node_commands
from nodes.state import _node_ws_connections

log = logging.getLogger("cloudbase.leftovers")


async def apps_there_are() -> dict:
    """{"keep": ids of the apps there are, "up_to": the highest id given out}, read at one moment."""
    async with AsyncSessionLocal() as db:
        keep = list((await db.execute(select(Application.id))).scalars())
        mark = (await db.execute(select(SystemConfig.value).where(SystemConfig.key == "applications.last_id"))).scalar_one_or_none()
        highest = (await db.execute(select(func.max(Application.id)))).scalar() or 0
    return {"keep": keep, "up_to": max(int(mark or 0), highest)}


async def sweep() -> None:
    async with AsyncSessionLocal() as db:
        nodes = list((await db.execute(select(Node.id).where(Node.is_local == False, Node.enabled == True))).scalars())  # noqa: E712
    there = await apps_there_are()
    await asyncio.to_thread(containers_cleanup.remove_leftovers, set(there["keep"]), there["up_to"])
    for node_id in nodes:
        if node_id not in _node_ws_connections:
            continue                                    # offline: the next round
        try:
            await node_commands.ask(node_id, "apps_sweep", there, timeout=60)
        except Exception as e:                          # also an agent from before this existed
            log.debug("[leftovers] node %s did not clean up: %s", node_id, e)
