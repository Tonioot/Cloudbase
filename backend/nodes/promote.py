"""Putting instances on other nodes into (and taking them out of) rotation, as the node network allows."""
import asyncio
import logging


from apps import health as app_health
from apps import nginx_sync
from core.database import AsyncSessionLocal
from core.models import Application, Instance, Node
from mesh import routing
from nodes.commands import _utcnow
from nodes.liveness import mark_instances_offline

log = logging.getLogger("cloudbase.nodes")

REACHABLE_TIMEOUT = 90
_promoting: set[int] = set()


async def promote_when_reachable(instance_id: int) -> None:
    """A started instance on another node goes live once the primary can connect to it."""
    if instance_id in _promoting:
        return
    _promoting.add(instance_id)
    try:
        try:
            backend = await routing.wait_reachable(instance_id, REACHABLE_TIMEOUT)      # "" for a worker (no port)
        except routing.Unreachable as exc:
            await _give_up(instance_id, str(exc))
            return
        if backend and not await _passes_health_check(instance_id, backend):
            return
        async with AsyncSessionLocal() as db:
            instance = await db.get(Instance, instance_id)
            if instance is None or instance.status != "starting":
                return                                  # a deploy or a stop got there first
            instance.status, instance.substatus, instance.last_error = "running", None, None
            instance.updated_at = _utcnow()
            app_id = instance.app_id
            await db.commit()
        log.info("[mesh] instance %d reachable, live", instance_id)
        await nginx_sync.refresh_app_nginx(app_id)
    except Exception as exc:
        log.warning("[mesh] promoting instance %d failed: %s", instance_id, exc)
    finally:
        _promoting.discard(instance_id)


async def _passes_health_check(instance_id: int, backend: str) -> bool:
    """Wait until the instance answers like the app's health check wants. False when it never does, or when
    someone else (a deploy, a stop) took over the instance meanwhile."""
    async with AsyncSessionLocal() as db:
        instance = await db.get(Instance, instance_id)
        app = await db.get(Application, instance.app_id) if instance else None
        if instance is None or app is None or instance.status != "starting":
            return False
        instance.substatus = "health_check"
        await db.commit()

    async def taken_over() -> str:
        async with AsyncSessionLocal() as db:
            current = await db.get(Instance, instance_id)
        if current is None or current.status != "starting":
            return "taken over"
        return ""

    healthy, detail = await app_health.wait_until_healthy(backend, app, taken_over)
    if healthy:
        return True
    if detail != "taken over":
        await _give_up(instance_id, f"health check failed: {detail}")
    return False


async def _give_up(instance_id: int, reason: str) -> None:
    async with AsyncSessionLocal() as db:
        instance = await db.get(Instance, instance_id)
        if instance is None or instance.status != "starting":
            return
        node = await db.get(Node, instance.node_id)
        # With the link down the node may simply be unreachable for a while: recovery retries later
        link_is_down = node is not None and not node.is_local and node.wg_state != "up"
        instance.status = "node_offline" if link_is_down else "error"
        instance.substatus = None
        instance.last_error = reason
        instance.updated_at = _utcnow()
        app_id = instance.app_id
        await db.commit()
    log.warning("[mesh] instance %d: %s", instance_id, reason)
    await nginx_sync.refresh_app_nginx(app_id)                  # no longer coming up: no start page for it


def start_promotion(instance_id: int) -> None:
    asyncio.get_running_loop().create_task(promote_when_reachable(instance_id))


async def link_down(node_id: int) -> None:
    """The node network to this node broke: its instances leave nginx and are recovered when it is back."""
    async with AsyncSessionLocal() as db:
        app_ids = await mark_instances_offline(db, node_id)
        await db.commit()
    for app_id in app_ids:
        await nginx_sync.refresh_app_nginx(app_id)
