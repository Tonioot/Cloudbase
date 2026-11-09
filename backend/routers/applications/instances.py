import asyncio
import json
from typing import List
from fastapi import Depends, APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import AsyncSessionLocal, get_db
from core import activity
from core.models import ApplicationReplica, Node
from containers import processes as pm
from nodes import desired
from nodes.registry import ensure_local_node
from nodes.commands import queue_node_command, wait_for_node_command
from core.env_crypto import decrypt_env
from core.audit import log_audit
from core import auth as _auth
from apps.nginx_sync import _has_public_nginx_domain, _write_app_nginx_config
from apps.runtime import _start_replica_runtime, _load_node_map, _derive_app_status_from_instances
from apps.serializers import _replica_to_dict, _get_or_404
from apps.state import locked

router = APIRouter(prefix="/api/apps", tags=["applications"])


@router.get("/{app_id}/instances")
async def list_instances(app_id: int, _user: dict = Depends(_auth.require_permission("apps.view")), db: AsyncSession = Depends(get_db)):
    """Return all instances (ApplicationReplica rows) for an app."""
    await _get_or_404(app_id, db)
    node_map = await _load_node_map(db)
    rep_result = await db.execute(
        select(ApplicationReplica).where(ApplicationReplica.app_id == app_id)
        .order_by(ApplicationReplica.id)
    )
    return [_replica_to_dict(r, node_map.get(r.node_id)) for r in rep_result.scalars().all()]


@router.get("/{app_id}/instances/stats")
async def get_instance_stats(app_id: int, _user: dict = Depends(_auth.require_permission("apps.view")), db: AsyncSession = Depends(get_db)):
    """Return the latest stats snapshot for each replica of this app."""
    await _get_or_404(app_id, db)
    pm.mark_watched(app_id)       # someone is looking: the collector samples this app every 2 s for a while
    rep_result = await db.execute(
        select(ApplicationReplica).where(ApplicationReplica.app_id == app_id)
    )
    replicas = rep_result.scalars().all()
    replica_ids = [r.id for r in replicas]
    snapshots = pm.get_all_replica_stats(app_id, replica_ids)

    # On-demand fallback: remote replicas without a cached snapshot get fetched in parallel.
    # We fire all commands concurrently so the total wait is bounded by one timeout, not N×timeout.
    missing_remote_running = [
        r for r in replicas
        if r.id not in snapshots and r.status in ("running", "starting") and r.node_id is not None
    ]
    if missing_remote_running:
        node_map = await _load_node_map(db)

        async def _fetch_one(r):
            node = node_map.get(r.node_id)
            if not node or node.is_local or node.status != "online":
                return
            try:
                async with AsyncSessionLocal() as fetch_db:
                    cmd = await queue_node_command(
                        fetch_db,
                        node_id=node.id,
                        app_id=r.app_id,
                        command_type="get_replica_stats",
                        payload={"app_id": r.app_id, "app_name": "", "replica_id": r.id},
                        allow_existing_inflight=True,
                    )
                    cmd_id = cmd.id
                async with AsyncSessionLocal() as wait_db:
                    done = await wait_for_node_command(wait_db, cmd_id, timeout_seconds=1.5)
                if done.status == "done" and done.result:
                    result = json.loads(done.result)
                    if result.get("cpu_percent") is not None:
                        pm.set_replica_stats(r.id, {"replica_id": r.id, **result})
                        snapshots[r.id] = {"replica_id": r.id, **result}
            except Exception:
                pass

        await asyncio.gather(*[_fetch_one(r) for r in missing_remote_running])

    return {
        str(rid): {
            "cpu_percent":  round(s.get("cpu_percent", 0), 1),
            "memory_mb":    round(s.get("memory_mb", 0), 0),
            "net_rx_mb":    round(s.get("net_rx_mb", 0), 2),
            "net_tx_mb":    round(s.get("net_tx_mb", 0), 2),
            "uptime_seconds": s.get("uptime_seconds"),
            "timestamp":    s.get("timestamp"),
        }
        for rid, s in snapshots.items()
    }


@router.post("/{app_id}/instances/{instance_id}/restart")
@locked("restarting")
@activity.tracked("restarting")
async def restart_instance(
    app_id: int,
    instance_id: int,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("apps.restart")),
    actor: str = Depends(_auth.get_current_actor),
):
    """Restart a single application instance."""
    app = await _get_or_404(app_id, db)
    rep_result = await db.execute(
        select(ApplicationReplica).where(
            ApplicationReplica.id == instance_id,
            ApplicationReplica.app_id == app_id,
        )
    )
    replica = rep_result.scalar_one_or_none()
    if not replica:
        raise HTTPException(404, "Instance not found")

    await _restart_replica(app, replica, db, actor)
    await db.commit()
    return {"status": replica.status, "instance_id": instance_id}


async def _restart_replica(app, replica, db, actor):
    node_result = await db.execute(select(Node).where(Node.id == replica.node_id)) if replica.node_id else None
    replica_node = node_result.scalar_one_or_none() if node_result else None

    local_node = await ensure_local_node(db)
    if replica_node is None:
        replica_node = local_node
    if not replica_node.is_local and replica_node.status != "online":
        raise HTTPException(400, f"Node '{replica_node.name}' is offline")

    # A new spec makes the node recreate the container: that is the restart
    await _start_replica_runtime(app, replica, replica_node, decrypt_env(app.env_vars or ""), db)

    await log_audit(db, "instance.restart", actor=actor, app_id=app.id,
                    detail={"name": app.name, "instance_id": replica.id})
    if _has_public_nginx_domain(app):
        await _write_app_nginx_config(app, db, local_node)



class InstanceIdsRequest(BaseModel):
    ids: List[int] = Field(..., min_length=1, max_length=50)


async def _replicas_of_app(app_id, ids, db):
    result = await db.execute(
        select(ApplicationReplica).where(ApplicationReplica.id.in_(ids), ApplicationReplica.app_id == app_id)
    )
    replicas = result.scalars().all()
    if len(replicas) != len(set(ids)):
        raise HTTPException(404, "One or more instances were not found")
    return replicas


@router.post("/{app_id}/instances/restart")
@locked("restarting")
@activity.tracked("restarting")
async def restart_instances(
    app_id: int,
    req: InstanceIdsRequest,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("apps.restart")),
    actor: str = Depends(_auth.get_current_actor),
):
    """Restart several instances of an app in one go."""
    app = await _get_or_404(app_id, db)
    replicas = await _replicas_of_app(app_id, req.ids, db)
    for replica in replicas:
        await _restart_replica(app, replica, db, actor)
    await db.commit()
    return {"status": "restarting", "instance_ids": sorted(r.id for r in replicas)}


@router.post("/{app_id}/instances/remove")
@locked("removing instances")
async def remove_instances(
    app_id: int,
    req: InstanceIdsRequest,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("apps.scale")),
    actor: str = Depends(_auth.get_current_actor),
):
    """Stop and remove several instances of an app in one go."""
    app = await _get_or_404(app_id, db)
    replicas = await _replicas_of_app(app_id, req.ids, db)
    await _remove_replicas(app, replicas, db, actor)
    await db.commit()
    return {"status": "deleted", "instance_ids": sorted(r.id for r in replicas)}


@router.delete("/{app_id}/instances/{instance_id}")
@locked("removing an instance")
async def delete_instance(
    app_id: int,
    instance_id: int,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("apps.scale")),
    actor: str = Depends(_auth.get_current_actor),
):
    """Stop and remove a single application instance."""
    app = await _get_or_404(app_id, db)
    rep_result = await db.execute(
        select(ApplicationReplica).where(
            ApplicationReplica.id == instance_id,
            ApplicationReplica.app_id == app_id,
        )
    )
    replica = rep_result.scalar_one_or_none()
    if not replica:
        raise HTTPException(404, "Instance not found")

    await _remove_replicas(app, [replica], db, actor)
    await db.commit()
    return {"status": "deleted", "instance_id": instance_id}


async def _remove_replicas(app, replicas, db, actor):
    # Nothing to send: the node (this server too) removes any container whose instance is no longer on its list,
    # also when it was offline just now and only hears about it when it is back.
    local_node = await ensure_local_node(db)
    removed = []
    for replica in replicas:
        desired.nudge(replica.node_id or local_node.id)
        # Always delete the DB row regardless of container state or node availability
        removed.append(replica.id)
        await db.delete(replica)
    await db.flush()

    remaining_result = await db.execute(
        select(ApplicationReplica).where(ApplicationReplica.app_id == app.id)
    )
    remaining = remaining_result.scalars().all()
    app.status = _derive_app_status_from_instances(remaining) if remaining else "stopped"

    # Regenerate nginx config without the removed backends.
    if _has_public_nginx_domain(app):
        await _write_app_nginx_config(app, db, local_node)

    for rid in removed:
        await log_audit(db, "instance.delete", actor=actor, app_id=app.id,
                        detail={"name": app.name, "instance_id": rid})
