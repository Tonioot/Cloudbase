"""Hands out how long app output is kept: to this machine and to every node, which prune their own copies.

A node keeps the last settings it got (containers/output_log.py), so it keeps pruning while the primary is away;
handing them out again every hour also reaches a node that was offline when they changed."""
import asyncio
import logging

from sqlalchemy import select

from containers import output_log
from core import config as _cfg
from core.database import AsyncSessionLocal
from core.models import Node
from nodes import commands as node_commands
from nodes.state import _node_ws_connections

log = logging.getLogger("cloudbase.output_log")

HAND_OUT_EVERY_SECONDS = 3600
FORGET_AGAIN_AFTER_SECONDS = 300


async def _connected_nodes() -> list[int]:
    async with AsyncSessionLocal() as db:
        rows = await db.execute(select(Node.id).where(Node.is_local == False, Node.enabled == True))  # noqa: E712
        return [node_id for node_id in rows.scalars().all() if node_id in _node_ws_connections]


async def hand_out() -> None:
    values = _cfg.get_logs()
    await asyncio.to_thread(output_log.set_settings, values)

    async def one(node_id: int) -> None:
        try:
            await node_commands.ask(node_id, "log_settings", values, timeout=15)
        except Exception as e:                       # offline, or an agent from before this existed: next round
            log.debug("[output] node %s did not take the log settings: %s", node_id, e)

    await asyncio.gather(*(one(node_id) for node_id in await _connected_nodes()))


async def forget_app(app_id: int) -> None:
    """A deleted app's output goes from this machine and every node."""
    await asyncio.to_thread(output_log.forget_app, app_id)
    for node_id in await _connected_nodes():
        try:
            await node_commands.ask(node_id, "logs_forget_app", {"app_id": app_id}, timeout=15)
        except Exception as e:
            log.debug("[output] node %s did not remove the output of app %s: %s", node_id, app_id, e)


def forget_app_later(app_id: int) -> None:
    """App ids can be reused: a new app must not show a deleted one's output. Removed now, and once more a few
    minutes later, after the nodes have removed the app's containers (each copies its last lines on the way out)."""
    async def run() -> None:
        await forget_app(app_id)
        await asyncio.sleep(FORGET_AGAIN_AFTER_SECONDS)
        await forget_app(app_id)
    asyncio.create_task(run())


async def log_settings_forever() -> None:
    await asyncio.sleep(30)                          # let the nodes connect first
    while True:
        try:
            await hand_out()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("[output] handing out log settings: %s", e)
        await asyncio.sleep(HAND_OUT_EVERY_SECONDS)
