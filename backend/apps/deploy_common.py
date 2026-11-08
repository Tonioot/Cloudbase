import asyncio
import contextlib
from fastapi import HTTPException
from core.database import AsyncSessionLocal
from core.models import Application, ApplicationReplica
from containers import processes as pm
from apps import health as app_health
from apps.state import DEPLOY_HEALTH_PROGRESS_SECONDS, _deploys_in_progress, _scaling_in_progress

# A new instance may first have to download the source and build the image
REMOTE_START_TIMEOUT_SECONDS = 900


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


async def _wait_new_instance_healthy(app_id: int, replica_id: int, backend: str) -> tuple[bool, str]:
    """Poll a freshly started deploy instance until it passes the app's health check.

    Returns (healthy, reason). Gives up early when its node reported the instance as failed.
    """
    async with AsyncSessionLocal() as _db:
        app = await _db.get(Application, app_id)
    loop = asyncio.get_running_loop()
    started = loop.time()
    next_progress = started + DEPLOY_HEALTH_PROGRESS_SECONDS
    timeout = app_health.settings_of(app)[2]

    async def stop_when() -> str:
        nonlocal next_progress
        async with AsyncSessionLocal() as _poll_db:
            r = await _poll_db.get(ApplicationReplica, replica_id)
        if r is None:
            return "instance was removed"
        if r.status == "error":
            return f"node reported an error: {r.last_error or 'unknown'}"
        if loop.time() >= next_progress:
            pm._push_line(app_id, f"[Deploy] Instance {replica_id} is not healthy yet after {int(loop.time() - started)}s — still waiting (max {timeout}s)…")
            next_progress += DEPLOY_HEALTH_PROGRESS_SECONDS
        return ""

    return await app_health.wait_until_healthy(backend, app, stop_when)
