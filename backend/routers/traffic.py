"""What every app sent to the public internet over a period: one call for billing (apps/traffic.py)."""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps import traffic as app_traffic
from core import auth as _auth
from core import projects as core_projects
from core.database import get_db
from core.models import Application

router = APIRouter(prefix="/api/traffic", tags=["traffic"])


@router.get("")
async def traffic_per_app(
    start: Optional[date] = Query(default=None, description="First day (UTC), YYYY-MM-DD"),
    end: Optional[date] = Query(default=None, description="Last day (UTC), YYYY-MM-DD; default today"),
    days: int = Query(default=30, ge=1, le=app_traffic.MAX_DAYS),
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_project_permission("apps.view")),
):
    """Per app, the biggest first: `internet_bytes`, `visitor_bytes` and `total_bytes` from start to end. Apps that
    were deleted meanwhile are included (`app_name` null) for whoever may see every app."""
    try:
        start, end = app_traffic.period(start, end, days)
    except ValueError as e:
        raise HTTPException(400, str(e))
    app_ids = None
    if core_projects.visible_projects(_user) is not None or not core_projects.can(_user, None, "apps.view"):
        app_ids = [a for a, p in (await db.execute(select(Application.id, Application.project_id))).all()
                   if core_projects.can(_user, p, "apps.view")]
    apps = await app_traffic.per_app(db, start, end, app_ids)
    return {"start": start.isoformat(), "end": end.isoformat(),
            "total_bytes": sum(a["total_bytes"] for a in apps), "apps": apps}
