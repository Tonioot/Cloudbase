"""The endpoint a Git host calls on every push. No sign-in: the signature of the delivery is the proof."""
import asyncio
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from core.database import AsyncSessionLocal
from core.models import Application
from apps import git as apps_git, webhooks as apps_webhooks
from apps.deployments import run_deploy

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

_MAX_BODY = 5 * 1024 * 1024


@router.post("/{app_id}")
async def receive(app_id: int, request: Request):
    body = await request.body()
    if len(body) > _MAX_BODY:
        raise HTTPException(413, "Too large")
    async with AsyncSessionLocal() as db:
        app = await db.get(Application, app_id)
        secret = apps_webhooks.secret_of(app) if app else None
        # Same answer for an unknown app, a switched-off webhook and a wrong signature
        provider = apps_webhooks.verify(request.headers, body, secret) if secret else None
        if not provider:
            raise HTTPException(401, "Invalid signature")
        auto_deploy = bool(app.auto_deploy)
        working_dir, deploy_branch, strategy = app.working_dir, app.deploy_branch, app.deploy_strategy or "rolling"
        revision = (app.source_revision or "").replace("-dirty", "")
    kind, push = apps_webhooks.parse_push(provider, request.headers, body)
    if kind == "ping":
        return {"status": "ok", "message": "Cloudbase received the ping"}
    if not push:
        return {"status": "ignored"}
    if not auto_deploy:
        return {"status": "ignored", "reason": "auto-deploy is off for this app"}
    branch, sha = push
    if not working_dir or not os.path.exists(working_dir):
        raise HTTPException(409, "The app has no source yet — deploy it once from the panel first")
    followed = deploy_branch or await asyncio.to_thread(apps_git._current_branch, working_dir)
    if branch != followed:
        return JSONResponse({"status": "ignored", "reason": f"the app follows {followed}, not {branch}"})
    if revision == sha:
        return {"status": "ignored", "reason": "this commit is already deployed"}
    asyncio.create_task(apps_webhooks.deploy_pushed(app_id, sha, run_deploy, strategy))
    return JSONResponse({"status": "deploying", "commit": sha}, status_code=202)
