import json
import asyncio
import logging
from sqlalchemy import select, and_, update
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import AsyncSessionLocal
from core.models import Node, NodeCommand
from nodes.commands import _utcnow, queue_node_command, wait_for_node_command
from nodes.state import _node_ws_connections, _push_node_event
from core.models import Application, ApplicationReplica
from core.env_crypto import decrypt_env
from apps.runtime import _remote_replica_command_payload
from containers import processes as pm
from containers import cleanup as containers_cleanup
from core import system_config as _syscfg
from apps.nginx_sync import _write_app_nginx_config
from apps.git import _refresh_app_source_revision as _rsr

log = logging.getLogger("cloudbase.nodes")


async def recover_node_replicas(node_id: int) -> None:
    """Queue start_replica for every replica marked node_offline on this node."""

    async with AsyncSessionLocal() as db:
        # An instance is only useful when the primary can reach it: wait for the node network.
        # (recover_stranded_replicas tries again as soon as the link is up.)
        node = await db.get(Node, node_id)
        if node is None or node.wg_state != "up":
            return
        rep_result = await db.execute(
            select(ApplicationReplica).where(
                and_(ApplicationReplica.node_id == node_id, ApplicationReplica.status == "node_offline")
            )
        )
        replicas = rep_result.scalars().all()
        if not replicas:
            return

        log.info("Node id=%d reconnected — recovering %d replica(s)", node_id, len(replicas))

        for replica in replicas:
            # Cancel any queued stop_replica for this replica — it was queued while the
            # node was offline and would undo the recovery start we're about to send.
            await db.execute(
                update(NodeCommand)
                .where(
                    and_(
                        NodeCommand.node_id == node_id,
                        NodeCommand.command_type == "stop_replica",
                        NodeCommand.status == "queued",
                        NodeCommand.payload.like(f'%"replica_id": {replica.id}%'),
                    )
                )
                .values(status="failed", error_message="Cancelled: node reconnected, recovery takes priority")
            )
            await db.commit()

            # Guard against queue storms during reconnect flapping:
            # if a start_replica command for this replica is already pending,
            # do not enqueue another one.
            pending_res = await db.execute(
                select(NodeCommand).where(
                    and_(
                        NodeCommand.node_id == node_id,
                        NodeCommand.command_type == "start_replica",
                        NodeCommand.status.in_(["queued", "in_progress"]),
                        NodeCommand.payload.like(f'%"replica_id": {replica.id}%'),
                    )
                ).limit(1)
            )
            if pending_res.scalar_one_or_none() is not None:
                continue

            app_result = await db.execute(select(Application).where(Application.id == replica.app_id))
            app = app_result.scalar_one_or_none()
            if not app:
                replica.status = "error"
                replica.last_error = "App not found during node recovery"
                await db.commit()
                continue

            # Always recover: node_offline only comes from a replica that was
            # running/starting when the node or the node network dropped, so running is
            # the desired state. (auto_start is about Cloudbase boot, not outages.)
            # If the container survived, the agent just keeps it.

            try:
                env_vars = decrypt_env(app.env_vars or "")
            except Exception:
                env_vars = {}

            ext_port = replica.external_port or app.port or 8000
            try:
                import asyncio as _asyncio
                source_revision = await _asyncio.to_thread(_rsr, app)
                remote_payload = _remote_replica_command_payload(app, env_vars, ext_port, source_revision)
            except Exception as _payload_exc:
                log.warning("recover_node_replicas: failed to build payload for replica %d: %s", replica.id, _payload_exc)
                replica.status = "error"
                replica.last_error = f"Recovery payload error: {_payload_exc}"
                await db.commit()
                continue

            replica.status = "starting"
            replica.last_error = None
            await db.commit()

            await queue_node_command(
                db,
                node_id=node_id,
                app_id=app.id,
                command_type="start_replica",
                payload={**remote_payload, "replica_id": replica.id},
            )
            pm._push_line(app.id, f"⟳ Node came back online — recovering instance {replica.id}…")
            await db.commit()


_recovering_nodes: set[int] = set()


async def _recover_node_replicas_once(node_id: int) -> None:
    if node_id in _recovering_nodes:
        return
    _recovering_nodes.add(node_id)
    try:
        await recover_node_replicas(node_id)
    except Exception as e:
        log.error("Replica recovery failed for node id=%d: %s", node_id, e)
    finally:
        _recovering_nodes.discard(node_id)


async def recover_stranded_replicas(db: AsyncSession) -> None:
    """Recover node_offline replicas on nodes that are connected again.

    Recovery on reconnect only fires when the node was marked offline at that
    moment. A node can also come back through a heartbeat on its existing
    websocket (e.g. after being marked stale during a long build), which left
    its replicas stuck in node_offline. This sweep catches every such case.
    """
    result = await db.execute(
        select(ApplicationReplica.node_id)
        .join(Node, Node.id == ApplicationReplica.node_id)
        .where(
            and_(
                ApplicationReplica.status == "node_offline",
                Node.status == "online",
                Node.enabled == True,
                Node.is_local == False,
                Node.wg_state == "up",
            )
        )
        .distinct()
    )
    for (node_id,) in result.all():
        if node_id in _node_ws_connections and node_id not in _recovering_nodes:
            asyncio.create_task(_recover_node_replicas_once(node_id))


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


# --- New endpoints ---

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


async def _regen_nginx_for_app(app_id: int) -> None:
    """Rebuild nginx config for an app after an instance went live or dropped out.

    Uses the shared applications writer so transition guards (start/restart pages)
    are applied consistently across all nginx write paths.
    """

    async with AsyncSessionLocal() as db:
        app_result = await db.execute(select(Application).where(Application.id == app_id))
        app = app_result.scalar_one_or_none()
        has_custom = bool(app and app.nginx_enabled and app.domain)
        has_auto = bool(_syscfg.get_base_domain_cached())
        if not app or not (has_custom or has_auto):
            return

        local_node_result = await db.execute(select(Node).where(Node.is_local == True))
        local_node = local_node_result.scalar_one_or_none()
        if not local_node:
            return
        try:
            await _write_app_nginx_config(app, db, local_node)
        except Exception as e:
            log.warning("[nginx] reload failed for app=%d: %s", app_id, e)
