import asyncio
import contextlib
from fastapi import HTTPException
from core.database import AsyncSessionLocal
from core.models import ApplicationReplica
from containers import processes as pm, replicas as containers_replicas
from apps.nginx_sync import _local_http_service_ready
from apps.state import DEPLOY_HEALTH_PROGRESS_SECONDS, _deploys_in_progress, _scaling_in_progress, DEPLOY_HEALTH_TIMEOUT_SECONDS

@contextlib.contextmanager
def _single_deploy(app_id: int):
    """Reject a second rolling / blue-green deploy while one is running for this app.

    These requests take minutes; without this a retry (or a second click after a
    proxy timeout) starts a parallel deploy that races the first one.
    """
    if app_id in _deploys_in_progress:
        raise HTTPException(409, "A deploy is already in progress for this app — wait for it to finish.")
    if app_id in _scaling_in_progress:
        raise HTTPException(409, "The autoscaler is adding or removing an instance for this app — try again in a moment.")
    _deploys_in_progress.add(app_id)
    try:
        yield
    finally:
        _deploys_in_progress.discard(app_id)


async def _wait_new_instance_healthy(app_id: int, replica_id: int, port: int, is_local: bool) -> tuple[bool, str]:
    """Poll a freshly started deploy instance until it serves HTTP.

    Returns (healthy, reason). Gives up early when the container has exited
    (local) or the node reported the replica as failed (remote).
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = started + DEPLOY_HEALTH_TIMEOUT_SECONDS
    next_progress = started + DEPLOY_HEALTH_PROGRESS_SECONDS
    while loop.time() < deadline:
        if await asyncio.to_thread(_local_http_service_ready, port):
            return True, ""
        if is_local:
            if not await asyncio.to_thread(containers_replicas.is_replica_container_running, app_id, replica_id):
                return False, "container is no longer running (check the instance logs)"
        else:
            async with AsyncSessionLocal() as _poll_db:
                r = await _poll_db.get(ApplicationReplica, replica_id)
            if r is None:
                return False, "instance was removed"
            if r.status == "error":
                return False, f"node reported an error: {r.last_error or 'unknown'}"
        if loop.time() >= next_progress:
            pm._push_line(app_id, f"[Deploy] Instance {replica_id} not responding yet after {int(loop.time() - started)}s — still waiting (max {DEPLOY_HEALTH_TIMEOUT_SECONDS}s)…")
            next_progress += DEPLOY_HEALTH_PROGRESS_SECONDS
        await asyncio.sleep(2)
    return False, f"no healthy HTTP response within {DEPLOY_HEALTH_TIMEOUT_SECONDS}s"
