import asyncio
import logging
from fastapi import Depends, APIRouter, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import AsyncSessionLocal, get_db
from core.models import ApplicationReplica, Node, Application
from nodes.registry import ensure_local_node
from nodes import desired
from core.env_crypto import decrypt_env
from core.audit import log_audit
from core import auth as _auth, config as _cfg
from apps.nginx_sync import _has_public_nginx_domain, _write_app_nginx_config
from apps.runtime import _start_instance_local, _remote_replica_command_payload, _set_replica_substatus, _docker_runtime_options
from apps.serializers import _replica_to_dict, _get_or_404
from apps.validation import _assign_external_port
from routers.applications.schemas import ScaleRequest

router = APIRouter(prefix="/api/apps", tags=["applications"])


log = logging.getLogger("cloudbase.apps")


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
        await desired.start(db, node_id=target_node.id, replica_id=replica.id, payload=remote_payload)
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
