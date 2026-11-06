"""Restarting an application: shared by the restart endpoint and by deploys."""

import asyncio
from typing import Optional
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from containers import processes as pm
from cron import scheduler as _cron
from nodes.registry import ensure_local_node
from nodes.commands import queue_node_command
from core.env_crypto import decrypt_env
from core.audit import log_audit
from apps.nginx_sync import _has_public_nginx_domain, _restore_nginx_after_transition, _write_app_nginx_config
from apps.runtime import _best_effort_stop_legacy_app_container, _derive_app_status_from_instances, _has_live_replicas, _load_app_replicas, _load_node_map, _remote_replica_command_payload, _resolve_replica_node, _start_replica_runtime
from apps.serializers import _get_or_404
from apps.state import _set_active_transition_mode


async def restart_app_now(app_id: int, db: AsyncSession, actor: str):
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

    await _best_effort_stop_legacy_app_container(app_id)

    for replica in replicas:
        replica_node = _resolve_replica_node(replica, node_map, local_node)
        if replica_node is None:
            replica.status = "error"
            replica.last_error = "Replica node not found"
            continue

        is_live = replica.status in ("running", "starting", "restarting", "stopping", "node_offline")
        if replica_node.is_local:
            if is_live:
                await asyncio.to_thread(pm.stop_docker_replica, app_id, replica.id)
            replica.status = "stopped"
            replica.container_id = None
            replica.tunnel_port = None
            await _start_replica_runtime(app, replica, replica_node, env_vars, db)
            continue

        if replica_node.status != "online" or not replica_node.enabled:
            replica.status = "error"
            replica.last_error = f"Node '{replica_node.name}' is offline"
            continue

        if is_live:
            await queue_node_command(
                db,
                node_id=replica_node.id,
                app_id=app.id,
                command_type="stop_replica",
                payload={"app_id": app.id, "replica_id": replica.id, "app_name": app.name},
            )
        remote_payload = _remote_replica_command_payload(
            app,
            env_vars,
            replica.external_port or app.port or 8000,
        )
        await queue_node_command(
            db,
            node_id=replica_node.id,
            app_id=app.id,
            command_type="start_replica",
            payload={**remote_payload, "replica_id": replica.id},
        )
        replica.status = "starting"
        replica.last_error = None

    app.status = _derive_app_status_from_instances(replicas)
    app.pid = None
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

    return {"status": app.status, "instance_count": len(replicas)}
