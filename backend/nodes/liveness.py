"""Which nodes are alive: marks silent nodes offline, and runs Docker cleanup on a node."""
import asyncio
import json
import logging

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from containers import cleanup as containers_cleanup
from core.database import AsyncSessionLocal
from core.models import ApplicationReplica, Node
from nodes.commands import _commit_with_sqlite_lock_retry, _utcnow, queue_node_command, wait_for_node_command
from nodes.state import _node_ws_connections, _push_node_event

log = logging.getLogger("cloudbase.nodes")


async def mark_instances_offline(db: AsyncSession, node_id: int) -> None:
    """The primary cannot reach this node: its live instances leave rotation and are picked up again when it is back
    (nodes/observed.py). The caller commits."""
    rows = await db.execute(select(ApplicationReplica).where(
        and_(ApplicationReplica.node_id == node_id, ApplicationReplica.status.in_(["running", "starting"]))))
    for replica in rows.scalars().all():
        replica.status, replica.substatus = "node_offline", None


async def mark_offline(db: AsyncSession, node: Node) -> bool:
    """The one place that decides what "this node is offline" means. False when it already was. The caller commits."""
    if node.status == "offline":
        return False
    node.status = "offline"
    if node.offline_since is None:
        node.offline_since = _utcnow()
    await mark_instances_offline(db, node.id)
    return True


async def node_gone(node_id: int) -> None:
    """The connection to a node is lost or dead: record it and tell the panel."""
    async with AsyncSessionLocal() as db:
        node = await db.get(Node, node_id)
        if node is not None and await mark_offline(db, node):
            await _commit_with_sqlite_lock_retry(db, context=f"node_gone node_id={node_id}")
            log.info("Node id=%d marked offline", node_id)
    _push_node_event(node_id, {"type": "node_offline", "node_id": node_id})


async def mark_stale_nodes_offline(db: AsyncSession) -> None:
    result = await db.execute(select(Node).where(and_(Node.enabled == True, Node.is_local == False)))  # noqa: E712
    now = _utcnow()
    gone = []
    for node in result.scalars().all():
        if not node.last_seen:
            continue
        threshold = max(node.heartbeat_interval * 2, 20)
        delta = (now - node.last_seen).total_seconds()
        if delta > threshold and await mark_offline(db, node):
            log.warning("Node '%s' (id=%d) stale: last seen %.0fs ago (threshold %ds) — marked offline", node.name, node.id, delta, threshold)
            gone.append(node.id)
    if gone:
        await db.commit()
        for node_id in gone:
            _push_node_event(node_id, {"type": "node_offline", "node_id": node_id})


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
