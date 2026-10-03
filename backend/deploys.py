"""Deploys and auto-deploy.

run_deploy() is the single path for putting a commit live: manual deploys
(the Deploy menu), auto-deploys (new commits on the watched branch) and
rollbacks all go through it, and every run is recorded as a Deployment.

auto_deploy_loop() polls the watched branch of every app with auto-deploy on
(`git ls-remote`, a few hundred bytes) and starts a deploy for a new commit.
"""
import asyncio
import logging
import os
import subprocess
import time
from datetime import datetime
from typing import Callable, Optional

from fastapi import HTTPException
from sqlalchemy import select

import process_manager as pm
from database import AsyncSessionLocal
from models import Application, Deployment

log = logging.getLogger("cloudbase.deploys")

_LOG_LIMIT_LINES = 3000


def _commit_details(app_dir: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """(sha, subject, author) of HEAD."""
    r = subprocess.run(["git", "log", "-1", "--format=%H%x1f%s%x1f%an"], cwd=app_dir, capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        return None, None, None
    sha, subject, author = (r.stdout.strip().split("\x1f") + ["", "", ""])[:3]
    return sha, subject[:500], author[:200]


async def run_deploy(
    app_id: int,
    *,
    commit: Optional[str] = None,
    strategy: str = "rolling",
    trigger: str = "manual",
    actor: str = "system",
    on_line: Optional[Callable[[str], None]] = None,
) -> dict:
    """Sync the source to `commit` (or the branch head) and put it live.

    strategy: rolling | blue_green | rebuild (build only). Apps without a
    public domain can't be rolled through nginx; they are rebuilt and their
    running instances restarted. Raises HTTPException on failure.
    """
    from routers import applications as apps  # late import: applications imports a lot

    def push(line: str) -> None:
        pm._push_line(app_id, str(line))

    # Collect every progress line (also those the rollout functions push
    # themselves) for the deployment's stored log
    lines_q = pm.subscribe_logs(app_id)
    captured: list[str] = []

    async def _collect():
        while True:
            line = await lines_q.get()
            if isinstance(line, str) and line.startswith("["):
                if len(captured) < _LOG_LIMIT_LINES:
                    captured.append(line)
                if on_line:
                    on_line(line)

    collector = asyncio.create_task(_collect())
    dep_id: Optional[int] = None
    try:
        with apps._single_deploy(app_id):
            async with AsyncSessionLocal() as db:
                app = await db.get(Application, app_id)
                if not app:
                    raise HTTPException(404, "App not found")
                if not app.working_dir or not os.path.exists(app.working_dir):
                    raise HTTPException(400, "No working directory — deploy the app first")

                # Without a public route nginx can't swap instances
                if strategy in ("rolling", "blue_green") and (app.no_web or not apps._has_public_nginx_domain(app)):
                    strategy = "restart"

                dep = Deployment(app_id=app_id, trigger=trigger, strategy=strategy, status="running",
                                 actor=actor, branch=app.deploy_branch)
                db.add(dep)
                await db.commit()
                dep_id = dep.id
                push(f"[Deploy] {trigger.capitalize()} deploy started ({strategy.replace('_', '/')})")

                commit_info, source_revision = await apps._sync_app_source(
                    app, app.working_dir, commit, push, branch=app.deploy_branch or None,
                )
                sha, subject, author = await asyncio.to_thread(_commit_details, app.working_dir)
                dep.commit_sha, dep.commit_message, dep.commit_author = sha, subject, author
                dep.branch = await asyncio.to_thread(apps._current_branch, app.working_dir)
                await db.commit()

                if strategy == "rolling":
                    push("[Deploy] Starting rolling deploy…")
                    res = await apps._do_rolling_deploy(app_id, db, actor)
                    message = f"Rolled out {commit_info}"
                elif strategy == "blue_green":
                    push("[Deploy] Starting blue/green deploy…")
                    local_node = await apps.ensure_local_node(db)
                    res = await apps._do_zero_downtime_deploy(app_id, db, local_node, actor)
                    message = f"Switched to {commit_info}"
                else:
                    res = await _rebuild(app, db, source_revision, push, restart=(strategy == "restart"), actor=actor)
                    message = (f"Deployed {commit_info}" if strategy == "restart" and res.get("restarted")
                               else f"Built {commit_info}")
                await apps.log_audit(db, "app.deploy", actor=actor, app_id=app_id,
                                     detail={"name": app.name, "commit": commit_info, "strategy": strategy, "trigger": trigger})
                await db.commit()
        await _finish(dep_id, "success", None, captured)
        return {"message": message, "commit": commit_info, "strategy": strategy, "deployment_id": dep_id, **(res or {})}
    except HTTPException as exc:
        await _finish(dep_id, "failed", str(exc.detail), captured)
        raise
    except Exception as exc:
        await _finish(dep_id, "failed", str(exc), captured)
        raise HTTPException(500, str(exc)) from exc
    finally:
        collector.cancel()
        pm.unsubscribe_logs(app_id, lines_q)


async def _rebuild(app: Application, db, source_revision, push, *, restart: bool, actor: str) -> dict:
    """Build the image; with `restart`, restart the app's running instances."""
    from routers import applications as apps
    import docker_manager as dm

    push("[Docker] Building image…")
    await asyncio.to_thread(
        dm.build_image,
        app.id, app.name, app.working_dir, lambda _aid, line: push(line),
        app.app_type or "unknown", app.start_command or "", app.port or 8000,
        **apps._image_build_kwargs(app),
    )
    app.docker_image = dm.image_name(app.id, app.name)
    if source_revision:
        app.image_revision = source_revision
    await db.commit()
    if not restart:
        push("[Docker] Image built. Restart or deploy to put it live.")
        return {}
    replicas = await apps._load_app_replicas(app.id, db)
    if not apps._has_live_replicas(replicas):
        push("[Deploy] App isn’t running — the new version starts the next time you start it.")
        return {"restarted": False}
    push("[Deploy] Restarting instances with the new image…")
    await apps.restart_app(app.id, db=db, _user={}, actor=actor)
    return {"restarted": True}


async def _finish(dep_id: Optional[int], status: str, error: Optional[str], captured: list[str]) -> None:
    if dep_id is None:
        return
    # Let the collector drain the last lines
    await asyncio.sleep(0.2)
    try:
        async with AsyncSessionLocal() as db:
            dep = await db.get(Deployment, dep_id)
            if dep:
                dep.status = status
                dep.error = (error or None) and error[:2000]
                dep.log = "\n".join(captured)
                dep.finished_at = datetime.utcnow()
                await db.commit()
    except Exception as e:
        log.warning("Could not record deployment %s: %s", dep_id, e)


# ── Auto-deploy ────────────────────────────────────────────────────────────

_last_check: dict[int, float] = {}
_failed_sha: dict[int, str] = {}   # don't retry a commit that already failed; a new push will
_LOOP_SECONDS = 15


def _remote_head(app: Application, branch: str) -> Optional[str]:
    from routers import applications as apps
    token = apps._decrypt_github_token(app.github_token)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        r = subprocess.run(
            ["git", "ls-remote", apps._build_clone_url(app.repo_url, token), f"refs/heads/{branch}"],
            capture_output=True, text=True, timeout=20, env=env,
        )
    except Exception:
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return r.stdout.split()[0]


async def _check_app(app_id: int) -> None:
    from routers import applications as apps
    async with AsyncSessionLocal() as db:
        app = await db.get(Application, app_id)
        if not app or not app.auto_deploy or not app.working_dir or not os.path.exists(app.working_dir):
            return
        branch = app.deploy_branch or await asyncio.to_thread(apps._current_branch, app.working_dir)
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
    if app_id in apps._deploys_in_progress:
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
