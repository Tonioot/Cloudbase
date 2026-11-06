"""Putting instances on other nodes into (and taking them out of) rotation, as the node network allows."""
import asyncio
import logging

from sqlalchemy import and_, select

from core.database import AsyncSessionLocal
from core.models import ApplicationReplica, Node
from mesh import routing
from nodes import commands
from nodes.commands import _utcnow
from nodes.recovery import _regen_nginx_for_app

log = logging.getLogger("cloudbase.nodes")

REACHABLE_TIMEOUT = 90
_promoting: set[int] = set()


async def promote_when_reachable(replica_id: int) -> None:
    """A started instance on another node goes live once the primary can connect to it."""
    if replica_id in _promoting:
        return
    _promoting.add(replica_id)
    try:
        try:
            await routing.wait_reachable(replica_id, REACHABLE_TIMEOUT)
        except routing.Unreachable as exc:
            await _give_up(replica_id, str(exc))
            return
        async with AsyncSessionLocal() as db:
            replica = await db.get(ApplicationReplica, replica_id)
            if replica is None or replica.status != "starting":
                return                                  # a deploy or a stop got there first
            replica.status, replica.substatus, replica.last_error = "running", None, None
            replica.updated_at = _utcnow()
            app_id = replica.app_id
            await db.commit()
        log.info("[mesh] instance %d reachable, live", replica_id)
        await _regen_nginx_for_app(app_id)
    except Exception as exc:
        log.warning("[mesh] promoting instance %d failed: %s", replica_id, exc)
    finally:
        _promoting.discard(replica_id)


async def _give_up(replica_id: int, reason: str) -> None:
    async with AsyncSessionLocal() as db:
        replica = await db.get(ApplicationReplica, replica_id)
        if replica is None or replica.status != "starting":
            return
        node = await db.get(Node, replica.node_id) if replica.node_id else None
        # With the link down the node may simply be unreachable for a while: recovery retries later
        link_is_down = node is not None and node.wg_state != "up"
        replica.status = "node_offline" if link_is_down else "error"
        replica.substatus = None
        replica.last_error = reason
        replica.updated_at = _utcnow()
        await db.commit()
    log.warning("[mesh] instance %d: %s", replica_id, reason)


def start_promotion(replica_id: int) -> None:
    asyncio.get_running_loop().create_task(promote_when_reachable(replica_id))


commands.on_replica_started(start_promotion)


async def link_down(node_id: int) -> None:
    """The node network to this node broke: its instances leave nginx and are recovered when it is back."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(ApplicationReplica).where(
            and_(ApplicationReplica.node_id == node_id, ApplicationReplica.status.in_(["running", "starting"]))))
        replicas = result.scalars().all()
        app_ids = {r.app_id for r in replicas}
        for replica in replicas:
            replica.status, replica.substatus = "node_offline", None
        await db.commit()
    for app_id in app_ids:
        await _regen_nginx_for_app(app_id)
