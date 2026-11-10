import asyncio
import json
import logging
import os
from fastapi import Body, HTTPException, Depends, APIRouter
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import get_db
from core.models import Deployment
from containers import processes as pm
from core import auth as _auth
from apps.serializers import _deployment_dict, _get_or_404
from apps.state import busy_with
from apps.validation import _check_git_ref
from routers.applications.schemas import GitDeployRequest
from routers.applications.sse import _DEPLOY_STREAM_DONE, _SSE_HEADERS, _SSE_PING, _queue_get_or_heartbeat, _sse_line
from apps import deployments as apps_deployments

router = APIRouter(prefix="/api/apps", tags=["applications"])


log = logging.getLogger("cloudbase.apps")


@router.post("/{app_id}/deploy-rolling")
async def deploy_rolling(app_id: int, _user: dict = Depends(_auth.require_permission("apps.pull")), actor: str = Depends(_auth.get_current_actor)):
    """Replace the instances one at a time with the current source (no new commit is fetched)."""
    return await apps_deployments.run_deploy(app_id, strategy="rolling", pull=False, actor=actor)


@router.post("/{app_id}/deploy-blue-green")
async def deploy_zero_downtime(app_id: int, _user: dict = Depends(_auth.require_permission("apps.pull")), actor: str = Depends(_auth.get_current_actor)):
    """New instances next to the old ones with the current source (no new commit is fetched)."""
    return await apps_deployments.run_deploy(app_id, strategy="blue_green", pull=False, actor=actor)


@router.get("/{app_id}/deployments")
async def list_deployments(app_id: int, limit: int = 50, db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("apps.view"))):
    await _get_or_404(app_id, db)
    rows = (await db.execute(
        select(Deployment).where(Deployment.app_id == app_id)
        .order_by(Deployment.created_at.desc(), Deployment.id.desc()).limit(max(1, min(limit, 200)))
    )).scalars().all()
    return {"deployments": [_deployment_dict(d) for d in rows], "in_progress": busy_with(app_id) == "deploying"}


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
    pull=false             keep the source as it is (Rebuild): no commit is fetched

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
    if busy_with(app_id):
        raise HTTPException(409, f"This app is busy ({busy_with(app_id)}) — try again when that is done.")

    target_commit = _check_git_ref(payload.commit)
    loop = asyncio.get_running_loop()
    lines = pm.subscribe_logs(app_id)
    result_holder: dict = {}

    def log(line: str) -> None:
        pm._push_line(app_id, str(line))

    async def _run() -> None:
        try:
            result_holder["result"] = await apps_deployments.run_deploy(
                app_id, commit=target_commit, strategy=strategy, pull=payload.pull,
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
