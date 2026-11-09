"""Starting and restarting an application: shared by the endpoints, deploys and the import."""

import asyncio
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from apps.git import _refresh_app_source_revision
from apps.nginx_sync import _has_public_nginx_domain, _restore_nginx_after_transition, _write_app_nginx_config
from apps.runtime import (
    _has_live_replicas,
    _load_app_replicas,
    _load_node_map,
    _resolve_replica_node,
    _start_replica_runtime,
)
from apps.serializers import _get_or_404, app_status
from apps.state import _set_active_transition_mode, app_lock, locked
from core import activity
from containers import processes as pm
from core.audit import log_audit
from core.env_crypto import decrypt_env
from cron import scheduler as _cron
from nodes.registry import ensure_local_node


async def restart_app_now(app_id: int, db: AsyncSession, actor: str):
    with app_lock(app_id, "restarting"):
        return await restart_locked(app_id, db, actor)


@activity.tracked("restarting")
async def restart_locked(app_id: int, db: AsyncSession, actor: str):
    """restart_app_now for a caller that already holds the app (a deploy that restarts what it just built)."""
    app = await _get_or_404(app_id, db)
    if app.cron_schedule:
        return await _cron.start_run(app_id, "manual", actor)
    local_node = await ensure_local_node(db)
    replicas = await _load_app_replicas(app_id, db)
    if not replicas:
        raise HTTPException(400, "No instances configured. Add an instance first before restarting the app.")
    env_vars = decrypt_env(app.env_vars or "")
    node_map = await _load_node_map(db)

    show_restart_page = (
        _has_live_replicas(replicas)
        and _has_public_nginx_domain(app)
        and not app.maintenance_mode
        and not app.update_mode
    )
    transition_started_at: Optional[float] = None
    if show_restart_page:
        transition_started_at = _set_active_transition_mode(app_id, "restart")
        await _write_app_nginx_config(app, db, local_node, mode="restart")
        pm._push_line(app_id, "Restarting instances...")

    source_revision = await asyncio.to_thread(_refresh_app_source_revision, app)
    for replica in replicas:
        # A new spec makes the node recreate the container: that is the restart
        await _start_replica_runtime(app, replica, _resolve_replica_node(replica, node_map, local_node),
                                     env_vars, db, source_revision)

    if _has_public_nginx_domain(app) and not show_restart_page:
        await _write_app_nginx_config(app, db, local_node)

    await log_audit(db, "app.restart", actor=actor, app_id=app_id, detail={"name": app.name})
    await db.commit()

    if show_restart_page and transition_started_at is not None:
        asyncio.create_task(
            _restore_nginx_after_transition(
                app_id,
                transition_started_at,
                "restart",
            )
        )

    return {"status": app_status(app, [r.status for r in replicas]), "instance_count": len(replicas)}


@locked("starting")
@activity.tracked("starting")
async def start_app_now(app_id: int, db: AsyncSession, actor: str):
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

    env_vars = decrypt_env(app.env_vars or "")
    source_revision = await asyncio.to_thread(_refresh_app_source_revision, app)  # also updates app.source_revision
    node_map = await _load_node_map(db)
    for replica in replicas:
        if replica.status in ("running", "starting", "restarting"):
            continue
        if replica.status not in ("stopped", "error", "pending", "node_offline"):
            continue
        replica_node = _resolve_replica_node(replica, node_map, local_node)
        await _start_replica_runtime(app, replica, replica_node, env_vars, db, source_revision)

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

    return {"status": app_status(app, [r.status for r in replicas]), "instance_count": len(replicas)}
