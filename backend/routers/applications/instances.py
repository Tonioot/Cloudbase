import asyncio
import json
from fastapi import Depends, APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import AsyncSessionLocal, get_db
from core.models import ApplicationReplica, Node
from containers import processes as pm
from nodes import desired
from nodes.registry import ensure_local_node
from nodes.commands import queue_node_command, wait_for_node_command
from core.env_crypto import decrypt_env
from core.audit import log_audit
from core import auth as _auth
from apps.nginx_sync import _has_public_nginx_domain, _write_app_nginx_config
from apps.runtime import _start_instance_local, _remote_replica_command_payload, _load_node_map, _derive_app_status_from_instances
from apps.serializers import _replica_to_dict, _get_or_404

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

    node_result = await db.execute(select(Node).where(Node.id == replica.node_id)) if replica.node_id else None
    replica_node = node_result.scalar_one_or_none() if node_result else None

    env_vars = decrypt_env(app.env_vars or "")

    if replica_node is None or replica_node.is_local:
        if replica.status == "running":
            await asyncio.to_thread(pm.stop_docker_replica, app_id, replica.id)
        await asyncio.sleep(1)
        try:
            cid = await _start_instance_local(app, replica, env_vars, app_id)
            replica.status = "running"
            replica.container_id = cid
            replica.last_error = None
        except HTTPException:
            raise
        except Exception as e:
            replica.status = "error"
            replica.last_error = str(e)
    elif replica_node.status == "online":
        remote_payload = _remote_replica_command_payload(
            app, env_vars, replica.external_port or app.port or 8000
        )
        await desired.start(db, node_id=replica_node.id, replica_id=replica.id, payload=remote_payload)
        replica.status = "starting"
    else:
        raise HTTPException(400, f"Node '{replica_node.name}' is offline")

    await log_audit(db, "instance.restart", actor=actor, app_id=app_id,
                    detail={"name": app.name, "instance_id": instance_id})
    if _has_public_nginx_domain(app):
        local_node = await ensure_local_node(db)
        await _write_app_nginx_config(app, db, local_node)
    await db.commit()
    return {"status": replica.status, "instance_id": instance_id}


@router.delete("/{app_id}/instances/{instance_id}")
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

    node_result = await db.execute(select(Node).where(Node.id == replica.node_id)) if replica.node_id else None
    replica_node = node_result.scalar_one_or_none() if node_result else None

    if replica_node is None or replica_node.is_local:
        # Always attempt stop for any non-terminal status — ignore errors if container
        # doesn't exist (deploying failed, already stopped, etc.)
        if replica.status not in ("stopped", "error"):
            try:
                await asyncio.to_thread(pm.stop_docker_replica, app_id, replica.id)
            except Exception:
                pass
    else:
        # Nothing to send: the node removes any container whose instance is no longer on its list,
        # also when it was offline just now and only hears about it when it is back.
        desired.nudge(replica_node.id)
    # Always delete the DB row regardless of container state or node availability

    await db.delete(replica)
    await db.flush()

    remaining_result = await db.execute(
        select(ApplicationReplica).where(ApplicationReplica.app_id == app_id)
    )
    remaining = remaining_result.scalars().all()
    app.status = _derive_app_status_from_instances(remaining) if remaining else "stopped"

    # Regenerate nginx config without the removed backend.
    if _has_public_nginx_domain(app):
        local_node = await ensure_local_node(db)
        await _write_app_nginx_config(app, db, local_node)

    await log_audit(db, "instance.delete", actor=actor, app_id=app_id,
                    detail={"name": app.name, "instance_id": instance_id})
    await db.commit()
    return {"status": "deleted", "instance_id": instance_id}
