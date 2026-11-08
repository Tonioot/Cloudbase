import logging
from fastapi import Depends, APIRouter, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import get_db
from core import activity
from core.models import ApplicationReplica, Node
from nodes.registry import ensure_local_node
from core.env_crypto import decrypt_env
from core.audit import log_audit
from core import auth as _auth, config as _cfg
from apps.runtime import _start_replica_runtime
from apps.serializers import _replica_to_dict, _get_or_404
from apps.validation import _assign_external_port
from routers.applications.schemas import ScaleRequest
from routers.applications.instances import _remove_replicas

router = APIRouter(prefix="/api/apps", tags=["applications"])


log = logging.getLogger("cloudbase.apps")


@router.post("/{app_id}/scale")
@activity.tracked("scaling")
async def scale_app(app_id: int, req: ScaleRequest, db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("apps.scale")), actor: str = Depends(_auth.get_current_actor)):
    if (await _get_or_404(app_id, db)).cron_schedule:
        raise HTTPException(400, "A cron job has no instances: each run starts its own container")
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)

    if req.target is not None:
        current = (await db.execute(
            select(ApplicationReplica).where(ApplicationReplica.app_id == app_id).order_by(ApplicationReplica.id)
        )).scalars().all()
        if req.target < len(current):
            # Fewer wanted: the newest instances go first
            gone = current[req.target:]
            await _remove_replicas(app, gone, db, actor)
            await db.commit()
            return {"instances": [], "removed": [r.id for r in gone], "requested": req.target, "error": None}
        if req.target == len(current):
            return {"instances": [], "removed": [], "requested": req.target, "error": None}
        req.count = req.target - len(current)

    instance_count_result = await db.execute(select(func.count()).select_from(ApplicationReplica))
    existing = instance_count_result.scalar()
    if existing + req.count > _cfg.get_limit("max_instances"):
        raise HTTPException(400, f"Instance limit reached ({_cfg.get_limit('max_instances')} instances maximum, {existing} in use). Adjust limits.max_instances in config.yaml to increase.")

    # Resolve target node for the new replica
    if req.node_id is not None:
        target_node_result = await db.execute(select(Node).where(Node.id == req.node_id, Node.enabled == True))
        target_node = target_node_result.scalar_one_or_none()
        if not target_node:
            raise HTTPException(400, "Target node not found or not enabled")
    else:
        target_node = local_node

    if not target_node.is_local and target_node.status != "online":
        raise HTTPException(400, f"Node '{target_node.name}' is not online")

    started = []
    error = None
    for _ in range(req.count):
        try:
            started.append(await _add_one(app, app_id, req, target_node, db, actor))
        except Exception as e:
            # Every instance is committed on its own, so the ones that did start stay
            await db.rollback()
            if not started:
                raise
            error = e.detail if isinstance(e, HTTPException) else str(e)
            log.warning("Scaling %s stopped after %d of %d: %s", app_id, len(started), req.count, error)
            break

    if req.count == 1 and req.target is None:
        return started[0]
    return {"instances": started, "requested": req.count, "error": error}


async def _add_one(app, app_id, req, target_node, db, actor):
    external_port = await _assign_external_port(None, target_node.id, db)

    replica = ApplicationReplica(
        app_id=app_id,
        node_id=target_node.id,
        external_port=external_port,
        status="pending",
    )
    db.add(replica)
    await db.flush()  # get replica.id before starting container

    # The instance's own limits stay on its row, so every later start uses them too
    replica.docker_cpu_limit = req.docker_cpu_limit
    replica.docker_memory_limit_mb = req.docker_memory_limit_mb
    replica.docker_read_only_root = bool(req.docker_read_only_root) if req.docker_read_only_root is not None else False
    replica.docker_tmpfs_enabled = bool(req.docker_tmpfs_enabled) if req.docker_tmpfs_enabled is not None else False
    replica.docker_tmpfs_size_mb = req.docker_tmpfs_size_mb
    await _start_replica_runtime(app, replica, target_node, decrypt_env(app.env_vars or ""), db)

    await log_audit(db, "app.scale_up", actor=actor, app_id=app_id, detail={"replica_id": replica.id, "node": target_node.name})
    await db.commit()
    await db.refresh(replica)

    return _replica_to_dict(replica, target_node)
