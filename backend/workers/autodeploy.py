import asyncio
import logging
import os
import subprocess
import time
from typing import Optional
from fastapi import HTTPException
from sqlalchemy import select
from core.database import AsyncSessionLocal
from core.models import Application, Deployment
from apps import state as apps_state, git as apps_git
from apps.deployments import run_deploy

log = logging.getLogger("cloudbase.deploys")


# ── Auto-deploy ────────────────────────────────────────────────────────────

_last_check: dict[int, float] = {}


_failed_sha: dict[int, str] = {}   # don't retry a commit that already failed; a new push will


_LOOP_SECONDS = 15


def _remote_head(app: Application, branch: str) -> Optional[str]:
    token = apps_git._decrypt_github_token(app.github_token)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        r = subprocess.run(
            ["git", "ls-remote", apps_git._build_clone_url(app.repo_url, token), f"refs/heads/{branch}"],
            capture_output=True, text=True, timeout=20, env=env,
        )
    except Exception:
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return r.stdout.split()[0]


async def _check_app(app_id: int) -> None:
    async with AsyncSessionLocal() as db:
        app = await db.get(Application, app_id)
        if not app or not app.auto_deploy or not app.working_dir or not os.path.exists(app.working_dir):
            return
        branch = app.deploy_branch or await asyncio.to_thread(apps_git._current_branch, app.working_dir)
        current = (app.source_revision or "").replace("-dirty", "")
    head = await asyncio.to_thread(_remote_head, app, branch)
    if not head or head == current or _failed_sha.get(app_id) == head:
        return
    # Only commits that were never deployed before: after a rollback the newer
    # commit stays off until the next push, and a commit that failed isn't
    # retried forever (also not after a restart).
    async with AsyncSessionLocal() as db:
        seen = (await db.execute(
            select(Deployment.id).where(
                Deployment.app_id == app_id,
                Deployment.commit_sha == head,
                Deployment.status.in_(["success", "failed"]),
            ).limit(1)
        )).first()
    if seen:
        return
    if app_id in apps_state._deploys_in_progress:
        return  # the next round picks up whatever is newest by then
    log.info("[auto-deploy] app %s: new commit %s on %s", app_id, head[:7], branch)
    try:
        await run_deploy(app_id, commit=head, strategy=app.deploy_strategy or "rolling",
                         trigger="auto", actor="auto-deploy")
        _failed_sha.pop(app_id, None)
    except HTTPException as e:
        _failed_sha[app_id] = head
        log.warning("[auto-deploy] app %s: deploy of %s failed: %s", app_id, head[:7], e.detail)


async def auto_deploy_loop() -> None:
    await asyncio.sleep(20)
    running: dict[int, asyncio.Task] = {}
    while True:
        try:
            async with AsyncSessionLocal() as db:
                rows = (await db.execute(
                    select(Application.id, Application.auto_deploy_interval).where(Application.auto_deploy == True)  # noqa: E712
                )).all()
            now = time.time()
            for app_id, interval in rows:
                if app_id in running and not running[app_id].done():
                    continue
                if now - _last_check.get(app_id, 0) < max(30, interval or 60):
                    continue
                _last_check[app_id] = now
                running[app_id] = asyncio.create_task(_check_app(app_id))
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.warning("[auto-deploy] loop error: %s", e)
        await asyncio.sleep(_LOOP_SECONDS)
