"""Cron jobs: schedule preview, runs, run now, pause/resume and settings."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core import auth as _auth
from cron import scheduler as cron
from core.database import get_db
from core.models import Application, CronRun

router = APIRouter(prefix="/api/apps", tags=["cron"])


class PreviewRequest(BaseModel):
    schedule: str
    timezone: Optional[str] = None


class CronSettingsRequest(BaseModel):
    schedule: Optional[str] = None
    timezone: Optional[str] = None       # "" = UTC
    timeout_seconds: Optional[int] = None
    node_id: Optional[int] = None        # where runs start; the primary when it is the local node


async def _cron_app(app_id: int, db: AsyncSession) -> Application:
    app = await db.get(Application, app_id)
    if app is None:
        raise HTTPException(404, "App not found")
    if not app.cron_schedule:
        raise HTTPException(400, "This app is not a cron job")
    return app


@router.post("/cron/preview")
async def preview(req: PreviewRequest, _user: dict = Depends(_auth.require_permission("apps.view"))):
    """The schedule in words and its next runs; 400 with the reason when it's invalid."""
    return cron.preview(req.schedule, req.timezone)


@router.get("/cron/options")
async def options(_user: dict = Depends(_auth.require_permission("apps.view"))):
    return {"timeouts": cron.TIMEOUT_CHOICES}


@router.get("/{app_id}/cron/runs")
async def list_runs(
    app_id: int,
    limit: int = 50,
    before: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("apps.view")),
):
    app = await _cron_app(app_id, db)
    q = select(CronRun).where(CronRun.app_id == app_id).order_by(CronRun.id.desc()).limit(max(1, min(limit, 200)))
    if before:
        q = q.where(CronRun.id < before)
    runs = (await db.execute(q)).scalars().all()
    return {"cron": cron.info(app), "status": app.status, "runs": [cron.run_dict(r) for r in runs]}


@router.get("/{app_id}/cron/runs/{run_id}")
async def get_run(
    app_id: int,
    run_id: int,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("apps.view")),
):
    run = await db.get(CronRun, run_id)
    if run is None or run.app_id != app_id:
        raise HTTPException(404, "Run not found")
    return cron.run_dict(run, with_output=True)


@router.post("/{app_id}/cron/run")
async def run_now(
    app_id: int,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("apps.start")),
    actor: str = Depends(_auth.get_current_actor),
):
    await _cron_app(app_id, db)
    return await cron.start_run(app_id, "manual", actor)


@router.post("/{app_id}/cron/pause")
async def pause(app_id: int, _user: dict = Depends(_auth.require_permission("apps.stop")),
                actor: str = Depends(_auth.get_current_actor)):
    return await cron.set_enabled(app_id, False, actor)


@router.post("/{app_id}/cron/resume")
async def resume(app_id: int, _user: dict = Depends(_auth.require_permission("apps.start")),
                 actor: str = Depends(_auth.get_current_actor)):
    return await cron.set_enabled(app_id, True, actor)


@router.put("/{app_id}/cron")
async def update_settings(
    app_id: int,
    req: CronSettingsRequest,
    _user: dict = Depends(_auth.require_permission("apps.configure")),
    actor: str = Depends(_auth.get_current_actor),
):
    return await cron.update_settings(app_id, actor, schedule=req.schedule, tz_name=req.timezone,
                                      timeout_seconds=req.timeout_seconds, node_id=req.node_id)
