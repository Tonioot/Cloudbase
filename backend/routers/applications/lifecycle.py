import asyncio
from fastapi import HTTPException, Depends, APIRouter
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import get_db
from containers import processes as pm
from cron import scheduler as _cron
from nodes.registry import ensure_local_node
from core.env_crypto import decrypt_env
from core.audit import log_audit
from core import auth as _auth
from apps.lifecycle import restart_app_now
from apps.git import _refresh_app_source_revision
from apps.nginx_sync import _has_public_nginx_domain, _restore_nginx_after_transition, _write_app_nginx_config
from apps.runtime import _best_effort_stop_legacy_app_container, _derive_app_status_from_instances, _has_live_replicas, _load_app_replicas, _load_node_map, _resolve_replica_node, _start_replica_runtime, _stop_replica_runtime
from apps.serializers import _get_or_404
from apps.state import _set_active_transition_mode

router = APIRouter(prefix="/api/apps", tags=["applications"])


@router.post("/{app_id}/start")
async def start_app(app_id: int, db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("apps.start")), actor: str = Depends(_auth.get_current_actor)):
    app = await _get_or_404(app_id, db)
    if app.cron_schedule:
        return await _cron.set_enabled(app_id, True, actor)
    local_node = await ensure_local_node(db)
    replicas = await _load_app_replicas(app_id, db)
    if not replicas:
        raise HTTPException(400, "No instances configured. Add an instance first before starting the app.")
    had_live_replicas = _has_live_replicas(replicas)
    show_start_page = (
        not had_live_replicas
        and _has_public_nginx_domain(app)
        and not app.maintenance_mode
        and not app.update_mode
    )

    if show_start_page:
        transition_started_at = _set_active_transition_mode(app_id, "starting")
        await _write_app_nginx_config(app, db, local_node, mode="starting")
        pm._push_line(app_id, "Starting instances...")

    await _best_effort_stop_legacy_app_container(app_id)

    env_vars = decrypt_env(app.env_vars or "")
    await asyncio.to_thread(_refresh_app_source_revision, app)  # also updates app.source_revision
    node_map = await _load_node_map(db)
    for replica in replicas:
        if replica.status in ("running", "starting", "restarting"):
            continue
        if replica.status not in ("stopped", "error", "pending", "node_offline"):
            continue
        replica_node = _resolve_replica_node(replica, node_map, local_node)
        await _start_replica_runtime(app, replica, replica_node, env_vars, db)

    app.status = _derive_app_status_from_instances(replicas)
    app.pid = None
    if _has_public_nginx_domain(app) and not show_start_page:
        await _write_app_nginx_config(app, db, local_node)

    await log_audit(db, "app.start", actor=actor, app_id=app_id, detail={"name": app.name})
    await db.commit()

    if show_start_page:
        asyncio.create_task(
            _restore_nginx_after_transition(
                app_id,
                transition_started_at,
                "start",
            )
        )

    return {"status": app.status, "instance_count": len(replicas)}


@router.post("/{app_id}/stop")
async def stop_app(app_id: int, db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("apps.stop")), actor: str = Depends(_auth.get_current_actor)):
    app = await _get_or_404(app_id, db)
    if app.cron_schedule:
        return await _cron.set_enabled(app_id, False, actor)
    local_node = await ensure_local_node(db)
    replicas = await _load_app_replicas(app_id, db)

    await _best_effort_stop_legacy_app_container(app_id)

    if not replicas:
        app.status = "stopped"
        app.pid = None
        if _has_public_nginx_domain(app):
            await _write_app_nginx_config(app, db, local_node)
        await log_audit(db, "app.stop", actor=actor, app_id=app_id, detail={"name": app.name})
        await db.commit()
        return {"status": "stopped", "instance_count": 0}

    node_map = await _load_node_map(db)
    for replica in replicas:
        if replica.status not in ("running", "starting", "restarting", "stopping", "node_offline"):
            continue
        replica_node = _resolve_replica_node(replica, node_map, local_node)
        await _stop_replica_runtime(app, replica, replica_node, db)

    app.status = _derive_app_status_from_instances(replicas)
    app.pid = None
    if _has_public_nginx_domain(app):
        await _write_app_nginx_config(app, db, local_node)

    await log_audit(db, "app.stop", actor=actor, app_id=app_id, detail={"name": app.name})
    await db.commit()
    return {"status": app.status, "instance_count": len(replicas)}


@router.post("/{app_id}/restart")
async def restart_app(app_id: int, db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("apps.restart")), actor: str = Depends(_auth.get_current_actor)):
    return await restart_app_now(app_id, db, actor)
