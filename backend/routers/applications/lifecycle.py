from fastapi import Depends, APIRouter
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import get_db
from core import activity
from cron import scheduler as _cron
from core.audit import log_audit
from core import auth as _auth
from apps.lifecycle import restart_app_now, start_app_now
from apps.nginx_sync import _has_public_nginx_domain, _write_app_nginx_config
from apps.runtime import _load_app_instances, _load_node_map, _stop_instance_runtime
from apps.serializers import _get_or_404, app_status
from apps.state import locked
from containers import processes as pm

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
    instances = await _load_app_instances(app_id, db)

    if not instances:
        if _has_public_nginx_domain(app):
            await _write_app_nginx_config(app, db)
        await log_audit(db, "app.stop", actor=actor, app_id=app_id, detail={"name": app.name})
        await db.commit()
        return {"status": "stopped", "instance_count": 0}

    node_map = await _load_node_map(db)
    stopping = [i for i in instances if i.status in ("running", "starting", "stopping", "node_offline")]
    pm._push_line(app_id, f"[Stop] Stopping {len(stopping)} instance(s) by {actor}")
    for instance in stopping:
        await _stop_instance_runtime(app, instance, node_map.get(instance.node_id), db)

    if _has_public_nginx_domain(app):
        await _write_app_nginx_config(app, db)

    await log_audit(db, "app.stop", actor=actor, app_id=app_id, detail={"name": app.name})
    await db.commit()
    return {"status": app_status(app, [r.status for r in instances]), "instance_count": len(instances)}


@router.post("/{app_id}/restart")
async def restart_app(app_id: int, db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("apps.restart")), actor: str = Depends(_auth.get_current_actor)):
    return await restart_app_now(app_id, db, actor)
