import logging
from fastapi import Depends, APIRouter, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import get_db
from core import activity
from core.models import Instance, Node
from nodes.registry import ensure_local_node
from core.env_crypto import decrypt_env
from core.audit import log_audit
from core import auth as _auth, config as _cfg
from apps.runtime import _start_instance_runtime
from apps.serializers import _instance_to_dict, _get_or_404
from apps.state import locked
from apps.validation import _assign_external_port
from routers.applications.schemas import ScaleRequest
from routers.applications.instances import _remove_instances

router = APIRouter(prefix="/api/apps", tags=["applications"])


log = logging.getLogger("cloudbase.apps")


@router.post("/{app_id}/scale")
@locked("scaling")
@activity.tracked("scaling")
async def scale_app(app_id: int, req: ScaleRequest, db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("apps.scale")), actor: str = Depends(_auth.get_current_actor)):
    if (await _get_or_404(app_id, db)).cron_schedule:
        raise HTTPException(400, "A cron job has no instances: each run starts its own container")
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)

    current = (await db.execute(
        select(Instance).where(Instance.app_id == app_id).order_by(Instance.id)
    )).scalars().all()
    if req.target is not None:
        if req.target < len(current):
            gone = pick_removals(current, len(current) - req.target, local_node.id)
            await _remove_instances(app, gone, db, actor)
            await db.commit()
            return {"instances": [], "removed": sorted(r.id for r in gone), "requested": req.target, "error": None}
        if req.target == len(current):
            return {"instances": [], "removed": [], "requested": req.target, "error": None}
        req.count = req.target - len(current)

    instance_count_result = await db.execute(select(func.count()).select_from(Instance))
    existing = instance_count_result.scalar()
    if existing + req.count > _cfg.get_limit("max_instances"):
        raise HTTPException(400, f"Instance limit reached ({_cfg.get_limit('max_instances')} instances maximum, {existing} in use). Adjust limits.max_instances in config.yaml to increase.")

    # A node picked by the caller is used as is; otherwise the new ones are spread over the nodes that can take them
    if req.node_id is not None:
        target_node_result = await db.execute(select(Node).where(Node.id == req.node_id, Node.enabled == True))
        pinned = target_node_result.scalar_one_or_none()
        if not pinned:
            raise HTTPException(400, "Target node not found or not enabled")
        if not pinned.is_local and pinned.status != "online":
            raise HTTPException(400, f"Node '{pinned.name}' is not online")
        candidates = [pinned]
    else:
        nodes = (await db.execute(select(Node).where(Node.enabled == True))).scalars().all()
        candidates = [n for n in nodes if n.is_local or n.status == "online"] or [local_node]
    counts = {}
    for r in current:
        counts[r.node_id] = counts.get(r.node_id, 0) + 1

    started = []
    error = None
    for _ in range(req.count):
        target_node = pick_node_for_new(candidates, counts)
        try:
            started.append(await _add_one(app, app_id, req, target_node, db, actor))
            counts[target_node.id] = counts.get(target_node.id, 0) + 1
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


def pick_node_for_new(candidates, counts):
    """The node with the fewest instances of this app; on a tie the primary, then the oldest node."""
    return min(candidates, key=lambda n: (counts.get(n.id, 0), 0 if n.is_local else 1, n.id))


def pick_removals(instances, n, local_node_id):
    """Which instances go when there are too many: not-running ones first, then from the node
    holding the most instances of this app (keeps the spread), then the newest."""
    left = list(instances)
    gone = []
    for _ in range(n):
        counts = {}
        for r in left:
            counts[r.node_id] = counts.get(r.node_id, 0) + 1
        health = lambda r: {"running": 2, "starting": 1}.get(r.status, 0)
        victim = min(left, key=lambda r: (health(r), -counts[r.node_id], -r.id))
        left.remove(victim)
        gone.append(victim)
    return gone


async def _add_one(app, app_id, req, target_node, db, actor):
    external_port = await _assign_external_port(None, target_node.id, db)

    instance = Instance(
        app_id=app_id,
        node_id=target_node.id,
        external_port=external_port,
        status="pending",
    )
    db.add(instance)
    await db.flush()  # get instance.id before starting container

    # The instance's own limits stay on its row, so every later start uses them too
    instance.docker_cpu_limit = req.docker_cpu_limit
    instance.docker_memory_limit_mb = req.docker_memory_limit_mb
    # Whatever the caller leaves out is the app's own setting
    instance.docker_read_only_root = bool(app.docker_read_only_root if req.docker_read_only_root is None else req.docker_read_only_root)
    instance.docker_tmpfs_enabled = bool(app.docker_tmpfs_enabled if req.docker_tmpfs_enabled is None else req.docker_tmpfs_enabled)
    instance.docker_tmpfs_size_mb = req.docker_tmpfs_size_mb
    await _start_instance_runtime(app, instance, target_node, decrypt_env(app.env_vars or ""), db)

    await log_audit(db, "app.scale_up", actor=actor, app_id=app_id, detail={"instance_id": instance.id, "node": target_node.name})
    await db.commit()
    await db.refresh(instance)

    return _instance_to_dict(instance, target_node)
