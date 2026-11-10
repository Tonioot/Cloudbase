"""Which nodes are alive: marks silent nodes offline, and runs Docker cleanup on a node."""
import asyncio
import logging

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps import nginx_sync
from containers import cleanup as containers_cleanup, processes as pm
from core.database import AsyncSessionLocal
from core.models import Instance, Node
from nodes import commands as node_commands
from nodes.commands import _commit_with_sqlite_lock_retry, _utcnow
from nodes.state import _node_ws_connections, _push_node_event

log = logging.getLogger("cloudbase.nodes")


async def mark_instances_offline(db: AsyncSession, node_id: int) -> set[int]:
    """The primary cannot reach this node: its live instances leave rotation and are picked up again when it is back
    (nodes/observed.py). Returns the apps whose nginx config must follow, once the caller has committed."""
    rows = await db.execute(select(Instance).where(
        and_(Instance.node_id == node_id, Instance.status.in_(["running", "starting"]))))
    app_ids = set()
    for instance in rows.scalars().all():
        instance.status, instance.substatus = "node_offline", None
        app_ids.add(instance.app_id)
        pm.instance_line(instance.app_id, instance.id, "node_offline")
    return app_ids


async def mark_offline(db: AsyncSession, node: Node) -> set[int] | None:
    """The one place that decides what "this node is offline" means. None when it already was; otherwise the apps
    whose nginx config must follow, once the caller has committed."""
    if node.status == "offline":
        return None
    node.status = "offline"
    if node.offline_since is None:
        node.offline_since = _utcnow()
    return await mark_instances_offline(db, node.id)


async def node_gone(node_id: int) -> None:
    """The connection to a node is lost or dead: record it and tell the panel."""
    app_ids: set[int] = set()
    async with AsyncSessionLocal() as db:
        node = await db.get(Node, node_id)
        affected = await mark_offline(db, node) if node is not None else None
        if affected is not None:
            await _commit_with_sqlite_lock_retry(db, context=f"node_gone node_id={node_id}")
            log.info("Node id=%d marked offline", node_id)
            app_ids = affected
    _push_node_event(node_id, {"type": "node_offline", "node_id": node_id})
    for app_id in app_ids:
        await nginx_sync.refresh_app_nginx(app_id)


async def mark_stale_nodes_offline(db: AsyncSession) -> None:
    result = await db.execute(select(Node).where(and_(Node.enabled == True, Node.is_local == False)))  # noqa: E712
    now = _utcnow()
    gone, app_ids = [], set()
    for node in result.scalars().all():
        if not node.last_seen:
            continue
        threshold = max(node.heartbeat_interval * 2, 20)
        delta = (now - node.last_seen).total_seconds()
        affected = await mark_offline(db, node) if delta > threshold else None
        if affected is not None:
            log.warning("Node '%s' (id=%d) stale: last seen %.0fs ago (threshold %ds) — marked offline", node.name, node.id, delta, threshold)
            gone.append(node.id)
            app_ids |= affected
    if gone:
        await db.commit()
        for node_id in gone:
            _push_node_event(node_id, {"type": "node_offline", "node_id": node_id})
        for app_id in app_ids:
            await nginx_sync.refresh_app_nginx(app_id)


async def run_node_docker_cleanup(node: Node, aggressive: bool = False) -> dict:
    """Clean up Docker on one node: in-process for the local node, through the
    agent for a remote one. Returns the cleanup summary dict."""
    if node.is_local:
        return await asyncio.to_thread(containers_cleanup.cleanup_docker, aggressive)
    if node.id not in _node_ws_connections:
        return {"skipped": "Node agent is not connected"}
    done = await node_commands.ask(node.id, "docker_cleanup", {"aggressive": aggressive}, timeout=300, reuse=True)
    if done.status != "done":
        return {"errors": [done.error_message or "Cleanup failed"]}
    return done.result or {}
