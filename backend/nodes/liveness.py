"""Which nodes are alive: marks silent nodes offline, and runs Docker cleanup on a node."""
import asyncio
import json
import logging

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from containers import cleanup as containers_cleanup
from core.database import AsyncSessionLocal
from core.models import ApplicationReplica, Node
from nodes.commands import _utcnow, queue_node_command, wait_for_node_command
from nodes.state import _node_ws_connections, _push_node_event

log = logging.getLogger("cloudbase.nodes")


async def mark_stale_nodes_offline(db: AsyncSession) -> None:
    result = await db.execute(select(Node).where(and_(Node.enabled == True, Node.is_local == False)))
    nodes = result.scalars().all()
    now = _utcnow()
    changed = False
    for node in nodes:
        if not node.last_seen:
            continue
        threshold = max(node.heartbeat_interval * 2, 20)
        delta = (now - node.last_seen).total_seconds()
        if delta > threshold and node.status != "offline":
            log.warning("Node '%s' (id=%d) stale: last seen %.0fs ago (threshold %ds) — marking offline", node.name, node.id, delta, threshold)
            node.status = "offline"
            if node.offline_since is None:
                node.offline_since = now
            node.connection_type = None
            changed = True
            _push_node_event(node.id, {"type": "node_offline", "node_id": node.id})
            # Mark running replicas as node_offline so they get recovered when the node reconnects
            rep_result = await db.execute(
                select(ApplicationReplica).where(
                    and_(
                        ApplicationReplica.node_id == node.id,
                        ApplicationReplica.status.in_(["running", "starting"]),
                    )
                )
            )
            for replica in rep_result.scalars().all():
                replica.status = "node_offline"
                replica.substatus = None
    if changed:
        await db.commit()


async def run_node_docker_cleanup(node: Node, aggressive: bool = False) -> dict:
    """Clean up Docker on one node: in-process for the local node, through the
    agent for a remote one. Returns the cleanup summary dict."""
    if node.is_local:
        return await asyncio.to_thread(containers_cleanup.cleanup_docker, aggressive)
    if node.id not in _node_ws_connections:
        return {"skipped": "Node agent is not connected"}
    async with AsyncSessionLocal() as db:
        cmd = await queue_node_command(
            db, node_id=node.id, command_type="docker_cleanup",
            payload={"aggressive": aggressive}, allow_existing_inflight=True,
        )
        cmd_id = cmd.id
    async with AsyncSessionLocal() as wait_db:
        done = await wait_for_node_command(wait_db, cmd_id, timeout_seconds=300)
    if done.status != "done":
        return {"errors": [done.error_message or "Cleanup failed"]}
    return json.loads(done.result) if done.result else {}
