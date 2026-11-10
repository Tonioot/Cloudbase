from typing import List
from fastapi import Depends, APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import get_db
from core import activity
from core.models import Instance, Node
from containers import processes as pm
from nodes import desired
from core.env_crypto import decrypt_env
from core.audit import log_audit
from core import auth as _auth
from apps.nginx_sync import _has_public_nginx_domain, _write_app_nginx_config
from apps.runtime import _start_instance_runtime, _load_node_map
from apps.serializers import _instance_to_dict, _get_or_404
from apps.state import locked

router = APIRouter(prefix="/api/apps", tags=["applications"])


@router.get("/{app_id}/instances")
async def list_instances(app_id: int, _user: dict = Depends(_auth.require_permission("apps.view")), db: AsyncSession = Depends(get_db)):
    """Return all instances (Instance rows) for an app."""
    await _get_or_404(app_id, db)
    node_map = await _load_node_map(db)
    rep_result = await db.execute(
        select(Instance).where(Instance.app_id == app_id)
        .order_by(Instance.id)
    )
    return [_instance_to_dict(r, node_map.get(r.node_id)) for r in rep_result.scalars().all()]


@router.get("/{app_id}/instances/stats")
async def get_instance_stats(app_id: int, _user: dict = Depends(_auth.require_permission("apps.view")), db: AsyncSession = Depends(get_db)):
    """Return the latest stats snapshot for each instance of this app."""
    await _get_or_404(app_id, db)
    pm.mark_watched(app_id)       # someone is looking: the collector samples this app every 2 s for a while
    rep_result = await db.execute(
        select(Instance).where(Instance.app_id == app_id)
    )
    instances = rep_result.scalars().all()
    instance_ids = [r.id for r in instances]
    snapshots = pm.get_all_instance_stats(app_id, instance_ids)

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
        select(Instance).where(
            Instance.id == instance_id,
            Instance.app_id == app_id,
        )
    )
    instance = rep_result.scalar_one_or_none()
    if not instance:
        raise HTTPException(404, "Instance not found")

    await _restart_instance(app, instance, db, actor)
    await db.commit()
    return {"status": instance.status, "instance_id": instance_id}


async def _restart_instance(app, instance, db, actor):
    instance_node = await db.get(Node, instance.node_id)
    if instance_node is None:
        raise HTTPException(409, "The node of this instance no longer exists")
    if not instance_node.is_local and instance_node.status != "online":
        raise HTTPException(400, f"Node '{instance_node.name}' is offline")

    # A new spec makes the node recreate the container: that is the restart
    await _start_instance_runtime(app, instance, instance_node, decrypt_env(app.env_vars or ""), db)

    await log_audit(db, "instance.restart", actor=actor, app_id=app.id,
                    detail={"name": app.name, "instance_id": instance.id})
    if _has_public_nginx_domain(app):
        await _write_app_nginx_config(app, db)



class InstanceIdsRequest(BaseModel):
    ids: List[int] = Field(..., min_length=1, max_length=50)


async def _instances_of_app(app_id, ids, db):
    result = await db.execute(
        select(Instance).where(Instance.id.in_(ids), Instance.app_id == app_id)
    )
    instances = result.scalars().all()
    if len(instances) != len(set(ids)):
        raise HTTPException(404, "One or more instances were not found")
    return instances


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
    instances = await _instances_of_app(app_id, req.ids, db)
    for instance in instances:
        await _restart_instance(app, instance, db, actor)
    await db.commit()
    return {"status": "restarting", "instance_ids": sorted(r.id for r in instances)}


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
    instances = await _instances_of_app(app_id, req.ids, db)
    await _remove_instances(app, instances, db, actor)
    await db.commit()
    return {"status": "deleted", "instance_ids": sorted(r.id for r in instances)}


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
        select(Instance).where(
            Instance.id == instance_id,
            Instance.app_id == app_id,
        )
    )
    instance = rep_result.scalar_one_or_none()
    if not instance:
        raise HTTPException(404, "Instance not found")

    await _remove_instances(app, [instance], db, actor)
    await db.commit()
    return {"status": "deleted", "instance_id": instance_id}


async def _remove_instances(app, instances, db, actor):
    # Nothing to send: the node (this server too) removes any container whose instance is no longer on its list,
    # also when it was offline just now and only hears about it when it is back.
    removed = []
    for instance in instances:
        desired.nudge(instance.node_id)
        # Always delete the DB row regardless of container state or node availability
        removed.append(instance.id)
        await db.delete(instance)
    await db.flush()

    # Regenerate nginx config without the removed backends.
    if _has_public_nginx_domain(app):
        await _write_app_nginx_config(app, db)

    for rid in removed:
        await log_audit(db, "instance.delete", actor=actor, app_id=app.id,
                        detail={"name": app.name, "instance_id": rid})
