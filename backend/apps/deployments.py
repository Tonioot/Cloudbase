import asyncio
import logging
import os
import subprocess
from datetime import datetime
from typing import Callable, Optional
from fastapi import HTTPException
from notify import emit as notify_emit
from containers import processes as pm, naming as containers_naming, build as containers_build
from core.database import AsyncSessionLocal
from core import activity
from core.models import Application, Deployment
from apps import state as apps_state, rolling as apps_rolling, bluegreen as apps_bluegreen, git as apps_git, nginx_sync as apps_nginx_sync, runtime as apps_runtime, images as apps_images
from core.audit import log_audit as apps_log_audit
from nodes.registry import ensure_local_node as apps_ensure_local_node
from apps.lifecycle import restart_locked

log = logging.getLogger("cloudbase.deploys")


_LOG_LIMIT_LINES = 3000


def _commit_details(app_dir: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """(sha, subject, author) of HEAD."""
    r = subprocess.run(["git", "log", "-1", "--format=%H%x1f%s%x1f%an"], cwd=app_dir, capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        return None, None, None
    sha, subject, author = (r.stdout.strip().split("\x1f") + ["", "", ""])[:3]
    return sha, subject[:500], author[:200]


@activity.tracked("deploying")
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
    app_name, commit_label = f"app {app_id}", None
    try:
        with apps_state.app_lock(app_id, "deploying"):
            async with AsyncSessionLocal() as db:
                app = await db.get(Application, app_id)
                if not app:
                    raise HTTPException(404, "App not found")
                app_name = app.name
                if not app.working_dir or not os.path.exists(app.working_dir):
                    raise HTTPException(400, "No working directory — deploy the app first")

                # Without a public route nginx can't swap instances
                if strategy in ("rolling", "blue_green") and (app.no_web or not apps_nginx_sync._has_public_nginx_domain(app)):
                    strategy = "restart"
                # A cron job has nothing running: build, and the next run uses it
                if app.cron_schedule:
                    strategy = "rebuild"

                dep = Deployment(app_id=app_id, trigger=trigger, strategy=strategy, status="running",
                                 actor=actor, branch=app.deploy_branch)
                db.add(dep)
                await db.commit()
                dep_id = dep.id
                push(f"[Deploy] {trigger.capitalize()} deploy started ({strategy.replace('_', '/')})")

                commit_info, source_revision = await apps_git._sync_app_source(
                    app, app.working_dir, commit, push, branch=app.deploy_branch or None,
                )
                sha, subject, author = await asyncio.to_thread(_commit_details, app.working_dir)
                commit_label = f"{sha[:7]} {subject or ''}".strip() if sha else None
                dep.commit_sha, dep.commit_message, dep.commit_author = sha, subject, author
                dep.branch = await asyncio.to_thread(apps_git._current_branch, app.working_dir)
                await db.commit()

                if strategy == "rolling":
                    push("[Deploy] Starting rolling deploy…")
                    res = await apps_rolling._do_rolling_deploy(app_id, db, actor)
                    message = f"Rolled out {commit_info}"
                elif strategy == "blue_green":
                    push("[Deploy] Starting blue/green deploy…")
                    local_node = await apps_ensure_local_node(db)
                    res = await apps_bluegreen._do_zero_downtime_deploy(app_id, db, local_node, actor)
                    message = f"Switched to {commit_info}"
                else:
                    res = await _rebuild(app, db, source_revision, push, restart=(strategy == "restart"), actor=actor)
                    message = (f"Deployed {commit_info}" if strategy == "restart" and res.get("restarted")
                               else f"Built {commit_info}")
                    if app.cron_schedule:
                        push("[Deploy] Image built. The next run of the job uses it.")
                        message = f"Deployed {commit_info}; the next run uses it"
                app.last_error = None
                await apps_log_audit(db, "app.deploy", actor=actor, app_id=app_id,
                                     detail={"name": app.name, "commit": commit_info, "strategy": strategy, "trigger": trigger})
                await db.commit()
        await _finish(dep_id, "success", None, captured)
        await _notify(app_id, app_name, trigger, commit_label, None)
        return {"message": message, "commit": commit_info, "strategy": strategy, "deployment_id": dep_id, **(res or {})}
    except HTTPException as exc:
        await _finish(dep_id, "failed", str(exc.detail), captured)
        if dep_id is not None:
            await _notify(app_id, app_name, trigger, commit_label, str(exc.detail))
        raise
    except Exception as exc:
        await _finish(dep_id, "failed", str(exc), captured)
        if dep_id is not None:
            await _notify(app_id, app_name, trigger, commit_label, str(exc))
        raise HTTPException(500, str(exc)) from exc
    finally:
        collector.cancel()
        pm.unsubscribe_logs(app_id, lines_q)


async def _rebuild(app: Application, db, source_revision, push, *, restart: bool, actor: str) -> dict:
    """Build the image; with `restart`, restart the app's running instances."""

    push("[Docker] Checking the image…")
    await asyncio.to_thread(
        containers_build.build_image_unless_current,
        app.id, app.working_dir, lambda _aid, line: push(line),
        app.app_type or "unknown", app.start_command or "", apps_images._image_port(app),
        **apps_images._image_build_kwargs(app),
    )
    app.docker_image = containers_naming.image_name(app.id)
    if source_revision:
        app.image_revision = source_revision
    await db.commit()
    if not restart:
        if not app.cron_schedule:
            push("[Docker] Image built. Restart or deploy to put it live.")
        return {}
    replicas = await apps_runtime._load_app_replicas(app.id, db)
    if not apps_runtime._has_live_replicas(replicas):
        push("[Deploy] App isn’t running — the new version starts the next time you start it.")
        return {"restarted": False}
    push("[Deploy] Restarting instances with the new image…")
    await restart_locked(app.id, db, actor)
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


async def _notify(app_id: int, app_name: str, trigger: str, commit: Optional[str], error: Optional[str]) -> None:
    what = {"auto": "Auto-deploy", "rollback": "Rollback"}.get(trigger, "Deploy")
    lines = [f"Commit: {commit}"] if commit else []
    if error is None:
        await notify_emit.emit("deploy.succeeded", f"{what} of {app_name} succeeded", "\n".join(lines),
                          app_id=app_id, link=f"/app?id={app_id}")
    elif "rolled back" in error:
        # The new instance failed its health check; the old version kept serving.
        await notify_emit.emit("deploy.rolled_back", f"{what} of {app_name} was rolled back",
                          "\n".join(lines + [error, "The previous version is still live."]),
                          app_id=app_id, link=f"/app?id={app_id}")
    else:
        await notify_emit.emit("deploy.failed", f"{what} of {app_name} failed", "\n".join(lines + [error]),
                          app_id=app_id, link=f"/app?id={app_id}")
