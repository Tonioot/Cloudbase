import asyncio
import json
import logging
import subprocess
from fastapi import HTTPException, Query, Depends, APIRouter
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import get_db, AsyncSessionLocal
from core.models import Application, ApplicationReplica, Node
from containers import processes as pm
from containers import naming as containers_naming
from containers import stats_logs as containers_stats_logs
from nodes.registry import ensure_local_node
from nodes.commands import queue_node_command, wait_for_node_command
from core.env_crypto import decrypt_env
from core.audit import log_audit
from core import auth as _auth, config as _cfg
from apps.nginx_sync import _has_public_nginx_domain, _write_app_nginx_config
from apps.replica_stats import local_app_aggregate_stats
from apps.runtime import _derive_app_status_from_instances, _docker_runtime_options, _load_node_map, _remote_replica_command_payload, _set_replica_substatus, _start_instance_local
from apps.serializers import _get_or_404, _replica_to_dict
from apps.validation import _assign_external_port
from routers.applications.deps import _require_node_agent_for_app
from routers.applications.schemas import RunReplicaRequest, ScaleRequest, _SubstatusUpdate

router = APIRouter(prefix="/api/apps", tags=["applications"])


log = logging.getLogger("cloudbase.apps")


@router.post("/{app_id}/replicas/run-remote")
async def run_replica_remote(app_id: int, req: RunReplicaRequest, _node: None = Depends(_require_node_agent_for_app()), db: AsyncSession = Depends(get_db)):
    """Internal endpoint called by the node agent to start a replica container locally.
    The agent guarantees the image is already built before calling this endpoint.
    Does NOT create a DB row — the main server already has it."""
    # app_name is always supplied by the agent — no local DB lookup needed
    if req.app_name:
        app_name = req.app_name
    else:
        app = await _get_or_404(app_id, db)
        app_name = app.name

    # Image was built under local_app_id (= main_id on remote nodes)
    build_app_id = req.local_app_id if req.local_app_id is not None else app_id

    try:
        env_vars = req.env_vars or {}
        container_id = await asyncio.to_thread(
            pm.start_docker_replica,
            app_id, req.replica_id, app_name,
            req.internal_port, req.external_port,
            env_vars, req.docker_options,
            build_app_id,
        )
        return {"container_id": container_id, "replica_id": req.replica_id}
    except Exception as e:
        import logging as _logging
        _logging.getLogger("cloudbase.apps").error(
            "run-remote failed app_id=%d replica_id=%d local_app_id=%s build_app_id=%d: %s",
            app_id, req.replica_id, req.local_app_id, build_app_id, e, exc_info=True,
        )
        raise HTTPException(500, str(e)) from e


@router.delete("/{app_id}/replicas/{replica_id}/stop-remote")
async def stop_replica_remote(app_id: int, replica_id: int, _node: None = Depends(_require_node_agent_for_app())):
    """Internal endpoint called by the node agent to stop a replica container locally."""
    ok = await asyncio.to_thread(pm.stop_docker_replica, app_id, replica_id)
    return {"ok": ok, "replica_id": replica_id}


@router.patch("/{app_id}/replicas/{replica_id}/substatus")
async def update_replica_substatus(
    app_id: int,
    replica_id: int,
    body: _SubstatusUpdate,
    _node: None = Depends(_require_node_agent_for_app()),
    db: AsyncSession = Depends(get_db),
):
    """Internal endpoint called by node agents to report startup progress."""
    await db.execute(
        update(ApplicationReplica)
        .where(ApplicationReplica.id == replica_id, ApplicationReplica.app_id == app_id)
        .values(substatus=body.substatus)
    )
    await db.commit()
    return {"ok": True}


@router.get("/{app_id}/replicas/aggregate-stats")
async def get_replica_aggregate_stats(app_id: int, _node: None = Depends(_require_node_agent_for_app())):
    """Internal endpoint called by the node agent to collect stats across all local
    replica containers for app_id.  Returns aggregated cpu/memory/net/disk numbers
    plus status=running when at least one container is up."""
    return await local_app_aggregate_stats(app_id)


@router.get("/{app_id}/replicas/{replica_id}/stats-remote")
async def get_replica_stats_remote(app_id: int, replica_id: int, _node: None = Depends(_require_node_agent_for_app())):
    """Internal endpoint called by node-agent to get one replica container's stats.

    This avoids app-level aggregate ambiguity when replica mapping is in transition.
    """
    import time as _time
    cname = containers_naming.replica_container_name(app_id, replica_id)
    stats = await asyncio.to_thread(containers_stats_logs.get_container_stats_by_name, cname)
    if not stats or stats.get("status") != "running":
        return {"status": "stopped", "docker": True}
    stats["timestamp"] = int(_time.time() * 1000)
    stats["replica_id"] = replica_id
    return stats


@router.get("/{app_id}/replicas")
async def list_replicas(app_id: int, _user: dict = Depends(_auth.require_permission("apps.view")), db: AsyncSession = Depends(get_db)):
    await _get_or_404(app_id, db)
    node_map = await _load_node_map(db)
    result = await db.execute(
        select(ApplicationReplica).where(ApplicationReplica.app_id == app_id)
    )
    replicas = result.scalars().all()
    return [_replica_to_dict(r, node_map.get(r.node_id)) for r in replicas]


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


@router.get("/{app_id}/replicas/{replica_id}/logs")
async def get_replica_logs(
    app_id: int,
    replica_id: int,
    lines: int = Query(200, ge=1, le=2000),
    db: AsyncSession = Depends(get_db),
):
    """Fetch recent log lines for a specific replica container.

    For local replicas: reads directly from docker logs.
    For remote replicas: queues a get_logs_tail agent command with the container name.
    """
    app = await _get_or_404(app_id, db)
    rep_result = await db.execute(
        select(ApplicationReplica).where(
            ApplicationReplica.id == replica_id,
            ApplicationReplica.app_id == app_id,
        )
    )
    replica = rep_result.scalar_one_or_none()
    if not replica:
        raise HTTPException(404, "Replica not found")

    node_result = await db.execute(select(Node).where(Node.id == replica.node_id)) if replica.node_id else None
    rep_node = (node_result.scalar_one_or_none() if node_result else None)

    container_name = containers_naming.replica_container_name(app_id, replica_id)

    if rep_node is None or rep_node.is_local:
        # Local — read directly
        try:
            raw = await asyncio.to_thread(
                lambda: subprocess.check_output(
                    ["docker", "logs", "--tail", str(lines), container_name],
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            )
            return {"lines": raw.splitlines(), "remote": False}
        except Exception as e:
            return {"lines": [], "remote": False, "error": str(e)}

    if rep_node.status != "online":
        return {"lines": [], "remote": True, "error": "Node is offline"}

    cmd = await queue_node_command(
        db,
        node_id=rep_node.id,
        app_id=app_id,
        command_type="get_replica_logs",
        payload={
            "app_id": app_id,
            "app_name": app.name,
            "replica_id": replica_id,
            "container_name": container_name,
            "lines": lines,
        },
    )
    done = await wait_for_node_command(db, cmd.id, timeout_seconds=20)
    if done.status != "done":
        return {"lines": [], "remote": True, "error": done.error_message}
    result_payload = json.loads(done.result or "{}") if done.result else {}
    return {"lines": result_payload.get("lines", []) or [], "remote": True}


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
        await queue_node_command(
            db, node_id=replica_node.id, app_id=app_id,
            command_type="stop_replica",
            payload={"app_id": app_id, "replica_id": replica.id, "app_name": app.name},
        )
        remote_payload = _remote_replica_command_payload(
            app, env_vars, replica.external_port or app.port or 8000
        )
        await queue_node_command(
            db, node_id=replica_node.id, app_id=app_id,
            command_type="start_replica",
            payload={**remote_payload, "replica_id": replica.id},
        )
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
        # Always queue a stop, whatever the status says: a "stopped" replica can
        # still have a running container (e.g. after a lost tunnel). The agent's
        # stop is a no-op when no container exists. Queued even when the node is
        # offline — it is dispatched as soon as the node reconnects.
        await queue_node_command(
            db, node_id=replica_node.id, app_id=app_id,
            command_type="stop_replica",
            payload={"app_id": app_id, "replica_id": replica.id, "app_name": app.name},
        )
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


@router.post("/{app_id}/scale")
async def scale_app(app_id: int, req: ScaleRequest, db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("apps.scale")), actor: str = Depends(_auth.get_current_actor)):
    if (await _get_or_404(app_id, db)).cron_schedule:
        raise HTTPException(400, "A cron job has no instances: each run starts its own container")
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)

    instance_count_result = await db.execute(select(func.count()).select_from(ApplicationReplica))
    if instance_count_result.scalar() >= _cfg.get_limit("max_instances"):
        raise HTTPException(400, f"Instance limit reached ({_cfg.get_limit('max_instances')} instances maximum). Adjust limits.max_instances in config.yaml to increase.")

    # Resolve target node for the new replica
    if req.node_id is not None:
        target_node_result = await db.execute(select(Node).where(Node.id == req.node_id, Node.enabled == True))
        target_node = target_node_result.scalar_one_or_none()
        if not target_node:
            raise HTTPException(400, "Target node not found or not enabled")
    else:
        target_node = local_node

    external_port = await _assign_external_port(None, target_node.id, None, db)

    replica = ApplicationReplica(
        app_id=app_id,
        node_id=target_node.id,
        external_port=external_port,
        status="pending",
    )
    db.add(replica)
    await db.flush()  # get replica.id before starting container

    env_vars = decrypt_env(app.env_vars or "")

    # Per-instance docker options override app-level defaults
    def _instance_docker_options() -> dict:
        base = _docker_runtime_options(app)
        if req.docker_cpu_limit is not None:
            base["cpu_limit"] = req.docker_cpu_limit
        if req.docker_memory_limit_mb is not None:
            base["memory_limit_mb"] = req.docker_memory_limit_mb
        if req.docker_read_only_root is not None:
            base["read_only_root"] = req.docker_read_only_root
        if req.docker_tmpfs_enabled is not None:
            base["tmpfs_enabled"] = req.docker_tmpfs_enabled
        if req.docker_tmpfs_size_mb is not None:
            base["tmpfs_size_mb"] = req.docker_tmpfs_size_mb
        return base

    if target_node.is_local:
        replica.docker_cpu_limit = req.docker_cpu_limit
        replica.docker_memory_limit_mb = req.docker_memory_limit_mb
        replica.docker_read_only_root = bool(req.docker_read_only_root) if req.docker_read_only_root is not None else False
        replica.docker_tmpfs_enabled = bool(req.docker_tmpfs_enabled) if req.docker_tmpfs_enabled is not None else False
        replica.docker_tmpfs_size_mb = req.docker_tmpfs_size_mb
        replica.status = "starting"

        replica_id_for_task = replica.id
        local_node_id = local_node.id
        has_nginx = _has_public_nginx_domain(app)

        async def _start_local_in_background():
            log.info("[scale] background task entered: replica_id=%s app_id=%s local_node_id=%s", replica_id_for_task, app_id, local_node_id)
            # Load data needed for the start — short read, session closed immediately.
            async with AsyncSessionLocal() as bg_db:
                bg_app = (await bg_db.execute(select(Application).where(Application.id == app_id))).scalar_one_or_none()
                bg_replica = (await bg_db.execute(select(ApplicationReplica).where(ApplicationReplica.id == replica_id_for_task))).scalar_one_or_none()
                bg_local_node = (await bg_db.execute(select(Node).where(Node.id == local_node_id))).scalar_one_or_none()
                if not bg_app or not bg_replica or not bg_local_node:
                    log.error("[scale] aborting: app=%s replica=%s node=%s", bg_app and bg_app.id, bg_replica, bg_local_node and bg_local_node.id)
                    return
                bg_env_vars = decrypt_env(bg_app.env_vars or "")

            log.info("[scale] starting local replica %d for app %d", replica_id_for_task, app_id)
            # Heavy Docker work runs with no DB session open — only _set_replica_substatus
            # opens short-lived sessions for substatus updates.
            try:
                container_id = await _start_instance_local(bg_app, bg_replica, bg_env_vars, app_id)
                log.info("[scale] local replica %d started, container=%s", replica_id_for_task, container_id)
                await _set_replica_substatus(replica_id_for_task, "waiting")
                # Write back: mark replica running + persist any image_revision update from the build
                async with AsyncSessionLocal() as bg_db:
                    done_replica = (await bg_db.execute(select(ApplicationReplica).where(ApplicationReplica.id == replica_id_for_task))).scalar_one_or_none()
                    done_app = (await bg_db.execute(select(Application).where(Application.id == app_id))).scalar_one_or_none()
                    if done_replica:
                        done_replica.status = "running"
                        done_replica.substatus = None
                        done_replica.container_id = container_id
                        done_replica.last_error = None
                        # _start_instance_local may have reassigned port on conflict
                        if bg_replica.external_port:
                            done_replica.external_port = bg_replica.external_port
                    if done_app:
                        # Persist image_revision/docker_image updates that _build_image() wrote
                        # onto the detached bg_app object.
                        if bg_app.image_revision:
                            done_app.image_revision = bg_app.image_revision
                        if bg_app.docker_image:
                            done_app.docker_image = bg_app.docker_image
                        if has_nginx:
                            done_node = (await bg_db.execute(select(Node).where(Node.id == local_node_id))).scalar_one_or_none()
                            if done_node:
                                await bg_db.flush()
                                await _write_app_nginx_config(done_app, bg_db, done_node)
                    await bg_db.commit()
            except Exception as e:
                log.error("[scale] local replica %d failed: %s", replica_id_for_task, e, exc_info=True)
                async with AsyncSessionLocal() as bg_db:
                    err_replica = (await bg_db.execute(select(ApplicationReplica).where(ApplicationReplica.id == replica_id_for_task))).scalar_one_or_none()
                    if err_replica:
                        err_replica.status = "error"
                        err_replica.substatus = None
                        err_replica.last_error = str(e)
                        await bg_db.commit()

    else:
        if target_node.status != "online":
            raise HTTPException(400, f"Node '{target_node.name}' is not online")
        remote_payload = _remote_replica_command_payload(app, env_vars, external_port)
        # Merge per-instance docker overrides into the payload
        inst_opts = _instance_docker_options()
        remote_payload["docker_cpu_limit"] = inst_opts.get("cpu_limit")
        remote_payload["docker_memory_limit_mb"] = inst_opts.get("memory_limit_mb")
        remote_payload["docker_read_only_root"] = inst_opts.get("read_only_root")
        remote_payload["docker_tmpfs_enabled"] = inst_opts.get("tmpfs_enabled")
        remote_payload["docker_tmpfs_size_mb"] = inst_opts.get("tmpfs_size_mb")
        await queue_node_command(
            db, node_id=target_node.id, app_id=app_id,
            command_type="start_replica",
            payload={**remote_payload, "replica_id": replica.id},
        )
        replica.status = "starting"

    await log_audit(db, "app.scale_up", actor=actor, app_id=app_id, detail={"replica_id": replica.id, "node": target_node.name})
    await db.commit()
    await db.refresh(replica)

    # Start background task AFTER commit so the replica row is visible to the new session.
    if target_node.is_local:
        _task = asyncio.create_task(_start_local_in_background())
        _task.add_done_callback(
            lambda t: log.error("[scale] background task crashed: %s", t.exception(), exc_info=t.exception())
            if not t.cancelled() and t.exception() else None
        )

    return _replica_to_dict(replica, target_node)


@router.delete("/{app_id}/replicas/{replica_id}")
async def remove_replica(app_id: int, replica_id: int, db: AsyncSession = Depends(get_db), actor: str = Depends(_auth.get_current_actor)):
    """Alias for DELETE /instances/{id} — kept for backwards compatibility."""
    return await delete_instance(app_id, replica_id, db=db, actor=actor)
