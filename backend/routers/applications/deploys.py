import asyncio
import json
import logging
import os
import subprocess
from typing import Optional
from fastapi import Body, HTTPException, Depends, APIRouter
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import get_db
from core.models import Deployment, ApplicationReplica, Node
from containers import processes as pm
from containers import naming as containers_naming
from containers import build as containers_build
from nodes.registry import ensure_local_node
from nodes.commands import queue_node_command
from core.audit import log_audit
from core import auth as _auth
from apps.deploy import _do_rolling_deploy, _do_zero_downtime_deploy, _single_deploy
from apps.git import _build_clone_url, _current_branch, _decrypt_github_token, _deploy_app, _fetch_origin, _refresh_app_source_revision, _resolve_source_revision, _sync_app_source
from apps.images import _image_build_kwargs, _image_port, _node_build_payload
from apps.serializers import _deployment_dict, _get_or_404
from apps.state import _deploys_in_progress
from apps.validation import _check_git_ref
from routers.applications.schemas import GitDeployRequest, PullRequest
from routers.applications.sse import _DEPLOY_STREAM_DONE, _SSE_HEADERS, _SSE_PING, _queue_get_or_heartbeat, _sse_line
import deploys

router = APIRouter(prefix="/api/apps", tags=["applications"])


log = logging.getLogger("cloudbase.apps")


@router.post("/{app_id}/pull")
async def git_pull(app_id: int, payload: PullRequest | None = Body(default=None), db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("apps.pull")), actor: str = Depends(_auth.get_current_actor)):
    app = await _get_or_404(app_id, db)
    target_commit = _check_git_ref(payload.commit if payload else None)
    app_dir = pm.get_app_dir(app.name)

    if not os.path.exists(app_dir):
        # The directory is missing! Let's try to restore it by re-deploying.
        log.warning("[git-pull] Directory %s missing for app %s, attempting re-clone", app_dir, app.name)
        try:
            await _deploy_app(app)
            await db.commit()
            return {
                "message": "App directory was missing; performed a fresh clone successfully",
                "output": "Fresh clone completed.",
                "source_revision": app.source_revision,
            }
        except Exception as e:
            raise HTTPException(500, f"App directory was missing and re-clone failed: {e}")

    github_token = _decrypt_github_token(app.github_token)

    def _git_ops() -> tuple[str, str, str, Optional[str]]:
        if github_token:
            url = _build_clone_url(app.repo_url, github_token)
            subprocess.run(["git", "remote", "set-url", "origin", url], cwd=app_dir, capture_output=True)
        branch = _current_branch(app_dir)
        _fetch_origin(app_dir, branch)
        target = target_commit or f"origin/{branch}"
        reset = subprocess.run(["git", "reset", "--hard", target], cwd=app_dir, capture_output=True, text=True)
        if reset.returncode != 0 and not target_commit:
            reset = subprocess.run(["git", "reset", "--hard", "@{u}"], cwd=app_dir, capture_output=True, text=True)
        if reset.returncode != 0:
            raise HTTPException(500, f"Git reset failed: {reset.stderr}")
        log_res = subprocess.run(["git", "log", "-1", "--format=%h - %s (%cr)"], cwd=app_dir, capture_output=True, text=True)
        commit_info = log_res.stdout.strip() if log_res.returncode == 0 else "Unknown"
        rev = _resolve_source_revision(app_dir)
        return branch, reset.stdout.strip(), commit_info, rev

    branch, _, commit_info, source_revision = await asyncio.to_thread(_git_ops)
    if source_revision:
        app.source_revision = source_revision

    was_running = pm.is_docker_app_running(app_id)
    action_logs: list[str] = [f"[Git] Updated code to {target_commit or branch} ({commit_info})"]
    action_logs.append("[Docker] Rebuilding image...")

    def _push(aid, line):
        _ = aid
        action_logs.append(str(line))

    try:
        await asyncio.to_thread(
            containers_build.build_image,
            app_id, app.name, app_dir, _push,
            app.app_type or "unknown", app.start_command or "",
            _image_port(app),
            **_image_build_kwargs(app),
        )
    except Exception as e:
        raise HTTPException(500, f"Failed to rebuild Docker image: {e}") from e

    app.docker_image = containers_naming.image_name(app_id, app.name)
    if source_revision:
        app.image_revision = source_revision
    await log_audit(db, "app.pull", actor=actor, app_id=app_id, detail={"name": app.name, "commit": commit_info})

    remote_replica_result = await db.execute(
        select(ApplicationReplica, Node)
        .join(Node, ApplicationReplica.node_id == Node.id)
        .where(ApplicationReplica.app_id == app_id, Node.is_local == False)
    )
    remote_nodes_notified: set[int] = set()
    for _, r_node in remote_replica_result.all():
        if r_node.id not in remote_nodes_notified:
            await queue_node_command(
                db, node_id=r_node.id, app_id=app_id,
                command_type="refresh_source",
                payload={
                    "app_id": app_id,
                    "app_name": app.name,
                    "commit": commit_info,
                    "source_revision": source_revision,
                    **_node_build_payload(app),
                },
            )
            action_logs.append(f"[Remote] Queued source refresh on node '{r_node.name}'.")
            remote_nodes_notified.add(r_node.id)

    await db.commit()
    if was_running:
        action_logs.append("[Docker] Image rebuilt. Running container left untouched (no stop/restart). Restart manually to use the new image.")
    else:
        action_logs.append("[Docker] Image rebuilt. Start the app to apply changes.")
    return {
        "message": f"Updated to {commit_info} and rebuilt the Docker image",
        "output": (
            f"Latest commit: {commit_info}\n\nImage rebuilt. Running container was not stopped or restarted. "
            "Restart manually when you want to switch to the new image."
            if was_running
            else f"Latest commit: {commit_info}\n\nImage rebuilt. Start the app to apply changes."
        ),
        "commit": commit_info,
        "source_revision": source_revision,
        "action_logs": action_logs,
    }


@router.post("/{app_id}/rebuild")
async def rebuild_docker_image(app_id: int, db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("apps.pull")), actor: str = Depends(_auth.get_current_actor)):
    app = await _get_or_404(app_id, db)

    if not app.working_dir:
        raise HTTPException(400, "No working directory — deploy the app first")

    was_running = pm.is_docker_app_running(app_id)
    action_logs: list[str] = ["[Docker] Rebuilding image..."]

    def _push(aid, line):
        _ = aid
        action_logs.append(str(line))

    try:
        img = await asyncio.to_thread(
            containers_build.build_image,
            app_id, app.name, app.working_dir, _push,
            app.app_type or "unknown", app.start_command or "", _image_port(app),
            **_image_build_kwargs(app),
        )
        source_revision = await asyncio.to_thread(_refresh_app_source_revision, app)
        app.status = "running" if was_running else "stopped"
        app.docker_image = img
        if source_revision:
            app.image_revision = source_revision
        await log_audit(db, "app.rebuild", actor=actor, app_id=app_id, detail={"name": app.name, "image": img})
        await db.commit()
        if was_running:
            action_logs.append("[Docker] Image rebuilt. Running container left untouched (no restart). Restart manually to use the new image.")
            return {
                "status": "running",
                "message": "Image rebuilt. Running container was not restarted.",
                "output": "Image rebuilt. Running container was not restarted. Restart manually to switch to the new image.",
                "source_revision": source_revision,
                "action_logs": action_logs,
            }
        action_logs.append("[Docker] Image rebuilt. Start the app to run it.")
        return {
            "status": "rebuilt",
            "message": "Image rebuilt. Start the app to run it.",
            "output": "Image rebuilt. Start the app to run it.",
            "source_revision": source_revision,
            "action_logs": action_logs,
        }
    except RuntimeError as e:
        msg = str(e)
        if "already in progress" in msg.lower():
            action_logs.append(f"[Docker] {msg}.")
            return {
                "status": "in_progress",
                "message": "A rebuild is already running for this app.",
                "output": "A rebuild is already running for this app. Wait for it to finish.",
                "action_logs": action_logs,
            }
        raise HTTPException(500, f"Failed to rebuild Docker image: {e}") from e
    except Exception as e:
        raise HTTPException(500, f"Failed to rebuild Docker image: {e}") from e


@router.post("/{app_id}/deploy-rolling")
async def deploy_rolling(app_id: int, db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("apps.pull")), actor: str = Depends(_auth.get_current_actor)):
    with _single_deploy(app_id):
        return await _do_rolling_deploy(app_id, db, actor)


@router.post("/{app_id}/deploy-blue-green")
async def deploy_zero_downtime(app_id: int, db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("apps.pull")), actor: str = Depends(_auth.get_current_actor)):
    await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)
    with _single_deploy(app_id):
        return await _do_zero_downtime_deploy(app_id, db, local_node, actor)


@router.post("/{app_id}/pull/stream")
async def git_pull_stream(app_id: int, payload: PullRequest | None = Body(default=None), _user: dict = Depends(_auth.require_permission("apps.pull")), db: AsyncSession = Depends(get_db)):
    """Streaming SSE variant of git_pull. Each build log line is emitted as it happens."""
    app = await _get_or_404(app_id, db)
    target_commit = _check_git_ref(payload.commit if payload else None)
    app_dir = pm.get_app_dir(app.name)

    if not os.path.exists(app_dir):
        async def _reclone_gen():
            yield _sse_line("[Git] App directory missing — attempting fresh clone…")
            try:
                await _deploy_app(app)
                yield _sse_line("[Git] Fresh clone completed.")
                yield f"event: result\ndata: {json.dumps({'message': 'Fresh clone completed', 'output': 'Fresh clone completed.'})}\n\n"
                yield "data: __DONE__\n\n"
            except Exception as exc:
                yield _sse_line(f"[Error] Re-clone failed: {exc}")
                yield "data: __FAILED__\n\n"
        return StreamingResponse(_reclone_gen(), media_type="text/event-stream", headers=_SSE_HEADERS)

    queue: asyncio.Queue = asyncio.Queue()
    result_holder: dict = {}
    loop = asyncio.get_running_loop()

    def _q(line: str) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, str(line))

    async def _do_pull() -> None:
        try:
            commit_info, source_revision = await _sync_app_source(app, app_dir, target_commit, _q)

            was_running = pm.is_docker_app_running(app_id)
            _q("[Docker] Rebuilding image…")

            def _docker_push(_aid, line):
                _q(str(line))

            await asyncio.to_thread(
                containers_build.build_image,
                app_id, app.name, app_dir, _docker_push,
                app.app_type or "unknown", app.start_command or "",
                _image_port(app),
                **_image_build_kwargs(app),
            )
            app.docker_image = containers_naming.image_name(app_id, app.name)
            if source_revision:
                app.image_revision = source_revision
            await db.commit()

            if was_running:
                _q("[Docker] Image rebuilt. Running container left untouched. Restart manually to use the new image.")
            else:
                _q("[Docker] Image rebuilt. Start the app to apply changes.")

            result_holder["result"] = {
                "message": f"Updated and rebuilt Docker image from {target_commit or _current_branch(app_dir)}",
                "commit": commit_info,
                "source_revision": source_revision,
                "output": (
                    "Image rebuilt. Restart manually to switch to the new image."
                    if was_running else
                    "Image rebuilt. Start the app to apply changes."
                ),
            }
        except HTTPException as exc:
            result_holder["error"] = exc.detail
        except Exception as exc:
            result_holder["error"] = str(exc)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    asyncio.create_task(_do_pull())

    async def _generate():
        while True:
            item = await _queue_get_or_heartbeat(queue)
            if item is _SSE_PING:
                yield ": ping\n\n"
                continue
            if item is None:
                if "error" in result_holder:
                    yield _sse_line(f"[Error] {result_holder['error']}")
                    yield "data: __FAILED__\n\n"
                else:
                    yield f"event: result\ndata: {json.dumps(result_holder.get('result', {}))}\n\n"
                    yield "data: __DONE__\n\n"
                break
            yield _sse_line(item)

    return StreamingResponse(_generate(), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.post("/{app_id}/rebuild/stream")
async def rebuild_docker_image_stream(app_id: int, _user: dict = Depends(_auth.require_permission("apps.pull")), db: AsyncSession = Depends(get_db)):
    """Streaming SSE variant of rebuild_docker_image."""
    app = await _get_or_404(app_id, db)

    if not app.working_dir:
        raise HTTPException(400, "No working directory — deploy the app first")

    queue: asyncio.Queue = asyncio.Queue()
    result_holder: dict = {}
    loop = asyncio.get_running_loop()
    was_running = pm.is_docker_app_running(app_id)

    def _q(line: str) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, str(line))

    async def _do_rebuild() -> None:
        try:
            _q("[Docker] Rebuilding image…")

            def _push(aid, line):
                _q(str(line))

            img = await asyncio.to_thread(
                containers_build.build_image,
                app_id, app.name, app.working_dir, _push,
                app.app_type or "unknown", app.start_command or "", _image_port(app),
                **_image_build_kwargs(app),
            )
            source_revision = _refresh_app_source_revision(app)
            app.status = "running" if was_running else "stopped"
            app.docker_image = img
            if source_revision:
                app.image_revision = source_revision
            await db.commit()

            if was_running:
                _q("[Docker] Image rebuilt. Running container left untouched. Restart manually to use the new image.")
                result_holder["result"] = {
                    "status": "running",
                    "message": "Image rebuilt. Running container was not restarted.",
                    "output": "Image rebuilt. Restart manually to switch to the new image.",
                    "source_revision": source_revision,
                }
            else:
                _q("[Docker] Image rebuilt. Start the app to run it.")
                result_holder["result"] = {
                    "status": "rebuilt",
                    "message": "Image rebuilt. Start the app to run it.",
                    "output": "Image rebuilt. Start the app to run it.",
                    "source_revision": source_revision,
                }
        except Exception as exc:
            result_holder["error"] = str(exc)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    asyncio.create_task(_do_rebuild())

    async def _generate():
        while True:
            item = await _queue_get_or_heartbeat(queue)
            if item is _SSE_PING:
                yield ": ping\n\n"
                continue
            if item is None:
                if "error" in result_holder:
                    yield _sse_line(f"[Error] {result_holder['error']}")
                    yield "data: __FAILED__\n\n"
                else:
                    yield f"event: result\ndata: {json.dumps(result_holder.get('result', {}))}\n\n"
                    yield "data: __DONE__\n\n"
                break
            yield _sse_line(item)

    return StreamingResponse(_generate(), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.get("/{app_id}/deployments")
async def list_deployments(app_id: int, limit: int = 50, db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("apps.view"))):
    await _get_or_404(app_id, db)
    rows = (await db.execute(
        select(Deployment).where(Deployment.app_id == app_id)
        .order_by(Deployment.created_at.desc(), Deployment.id.desc()).limit(max(1, min(limit, 200)))
    )).scalars().all()
    return {"deployments": [_deployment_dict(d) for d in rows], "in_progress": app_id in _deploys_in_progress}


@router.get("/{app_id}/deployments/{deployment_id}")
async def get_deployment(app_id: int, deployment_id: int, db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("apps.view"))):
    d = await db.get(Deployment, deployment_id)
    if not d or d.app_id != app_id:
        raise HTTPException(404, "Deployment not found")
    return _deployment_dict(d, with_log=True)


@router.post("/{app_id}/deploy/stream")
async def deploy_stream(
    app_id: int,
    payload: GitDeployRequest | None = Body(default=None),
    _user: dict = Depends(_auth.require_permission("apps.pull")),
    actor: str = Depends(_auth.get_current_actor),
    db: AsyncSession = Depends(get_db),
):
    """Sync source to a commit, then build — and optionally roll it out.

    strategy="rebuild"     build a new image; running instances are left alone
    strategy="rolling"     replace instances one at a time with the new image
    strategy="blue_green"  start new instances next to the old ones, then switch

    Rolling and blue/green build the image themselves, so the code is only built
    once. Progress lines (those starting with "[") are streamed as SSE.
    """
    payload = payload or GitDeployRequest()
    strategy = payload.strategy
    if strategy not in ("rebuild", "rolling", "blue_green"):
        raise HTTPException(400, "strategy must be one of: rebuild, rolling, blue_green")
    app = await _get_or_404(app_id, db)
    if not app.working_dir or not os.path.exists(app.working_dir):
        raise HTTPException(400, "No working directory — deploy the app first")
    if app_id in _deploys_in_progress:
        raise HTTPException(409, "A deploy is already in progress for this app — wait for it to finish.")

    target_commit = _check_git_ref(payload.commit)
    loop = asyncio.get_running_loop()
    lines = pm.subscribe_logs(app_id)
    result_holder: dict = {}

    def log(line: str) -> None:
        pm._push_line(app_id, str(line))

    async def _run() -> None:
        try:
            result_holder["result"] = await deploys.run_deploy(
                app_id, commit=target_commit, strategy=strategy,
                trigger="rollback" if payload.trigger == "rollback" else "manual", actor=actor,
            )
        except HTTPException as exc:
            result_holder["error"] = exc.detail
        except Exception as exc:
            result_holder["error"] = str(exc)
        finally:
            loop.call_soon_threadsafe(lines.put_nowait, _DEPLOY_STREAM_DONE)

    asyncio.create_task(_run())

    async def _generate():
        try:
            while True:
                item = await _queue_get_or_heartbeat(lines)
                if item is _SSE_PING:
                    yield ": ping\n\n"
                    continue
                if item is _DEPLOY_STREAM_DONE:
                    if "error" in result_holder:
                        yield _sse_line(f"[Error] {result_holder['error']}")
                        yield "data: __FAILED__\n\n"
                    else:
                        yield f"event: result\ndata: {json.dumps(result_holder.get('result', {}))}\n\n"
                        yield "data: __DONE__\n\n"
                    break
                # Only deploy progress — container output stays in the Logs tab
                if isinstance(item, str) and item.startswith("["):
                    yield _sse_line(item)
        finally:
            pm.unsubscribe_logs(app_id, lines)

    return StreamingResponse(_generate(), media_type="text/event-stream", headers=_SSE_HEADERS)
