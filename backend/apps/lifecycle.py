"""Starting and restarting an application: shared by the endpoints, deploys and the import."""

import asyncio

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from apps.git import _refresh_app_source_revision
from apps.nginx_sync import _has_public_nginx_domain, _write_app_nginx_config
from apps.runtime import _load_app_instances, _load_node_map, _start_instance_runtime
from apps.serializers import _get_or_404, app_status
from apps.state import app_lock, locked
from core import activity
from containers import processes as pm
from core.audit import log_audit
from core.env_crypto import decrypt_env
from cron import scheduler as _cron


async def restart_app_now(app_id: int, db: AsyncSession, actor: str):
    with app_lock(app_id, "restarting"):
        return await restart_locked(app_id, db, actor)


@activity.tracked("restarting")
async def restart_locked(app_id: int, db: AsyncSession, actor: str):
    """restart_app_now for a caller that already holds the app (a deploy that restarts what it just built)."""
    app = await _get_or_404(app_id, db)
    if app.cron_schedule:
        return await _cron.start_run(app_id, "manual", actor)
    instances = await _load_app_instances(app_id, db)
    if not instances:
        raise HTTPException(400, "No instances configured. Add an instance first before restarting the app.")
    pm._push_line(app_id, "Restarting instances...")
    # A new spec makes the node recreate the container: that is the restart
    await _start(app, instances, db)
    await log_audit(db, "app.restart", actor=actor, app_id=app_id, detail={"name": app.name})
    await db.commit()
    return {"status": app_status(app, [r.status for r in instances]), "instance_count": len(instances)}


@locked("starting")
@activity.tracked("starting")
async def start_app_now(app_id: int, db: AsyncSession, actor: str):
    app = await _get_or_404(app_id, db)
    if app.cron_schedule:
        return await _cron.set_enabled(app_id, True, actor)
    instances = await _load_app_instances(app_id, db)
    if not instances:
        raise HTTPException(400, "No instances configured. Add an instance first before starting the app.")
    pm._push_line(app_id, "Starting instances...")
    await _start(app, [r for r in instances if r.status in ("stopped", "error", "pending", "node_offline")], db)
    await log_audit(db, "app.start", actor=actor, app_id=app_id, detail={"name": app.name})
    await db.commit()
    return {"status": app_status(app, [r.status for r in instances]), "instance_count": len(instances)}


async def _start(app, instances, db: AsyncSession) -> None:
    """Put these instances on their nodes' lists with the current spec, then let nginx follow: until one of the
    app's instances is live it shows the start or restart page (apps/nginx_sync.py), after that the app."""
    env_vars = decrypt_env(app.env_vars or "")
    source_revision = await asyncio.to_thread(_refresh_app_source_revision, app)  # also updates app.source_revision
    node_map = await _load_node_map(db)
    for instance in instances:
        await _start_instance_runtime(app, instance, node_map.get(instance.node_id), env_vars, db, source_revision)
    if _has_public_nginx_domain(app):
        await _write_app_nginx_config(app, db)
