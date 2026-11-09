from fastapi import Depends, APIRouter
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import get_db
from core import activity
from cron import scheduler as _cron
from nodes.registry import ensure_local_node
from core.audit import log_audit
from core import auth as _auth
from apps.lifecycle import restart_app_now, start_app_now
from apps.nginx_sync import _has_public_nginx_domain, _write_app_nginx_config
from apps.runtime import _derive_app_status_from_instances, _load_app_replicas, _load_node_map, _resolve_replica_node, _stop_replica_runtime
from apps.serializers import _get_or_404
from apps.state import locked

router = APIRouter(prefix="/api/apps", tags=["applications"])


@router.post("/{app_id}/start")
async def start_app(app_id: int, db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("apps.start")), actor: str = Depends(_auth.get_current_actor)):
    return await start_app_now(app_id, db, actor)


@router.post("/{app_id}/stop")
@locked("stopping")
@activity.tracked("stopping")
async def stop_app(app_id: int, db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("apps.stop")), actor: str = Depends(_auth.get_current_actor)):
    app = await _get_or_404(app_id, db)
    if app.cron_schedule:
        return await _cron.set_enabled(app_id, False, actor)
    local_node = await ensure_local_node(db)
    replicas = await _load_app_replicas(app_id, db)

    if not replicas:
        app.status = "stopped"
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
    if _has_public_nginx_domain(app):
        await _write_app_nginx_config(app, db, local_node)

    await log_audit(db, "app.stop", actor=actor, app_id=app_id, detail={"name": app.name})
    await db.commit()
    return {"status": app.status, "instance_count": len(replicas)}


@router.post("/{app_id}/restart")
async def restart_app(app_id: int, db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("apps.restart")), actor: str = Depends(_auth.get_current_actor)):
    return await restart_app_now(app_id, db, actor)
