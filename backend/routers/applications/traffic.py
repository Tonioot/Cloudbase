"""What one app sent to the public internet, per day (apps/traffic.py)."""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from apps import traffic as app_traffic
from apps.serializers import _get_or_404
from core import auth as _auth
from core.database import get_db

router = APIRouter(prefix="/api/apps", tags=["applications"])


@router.get("/{app_id}/traffic")
async def app_traffic_days(
    app_id: int,
    start: Optional[date] = Query(default=None, description="First day (UTC), YYYY-MM-DD"),
    end: Optional[date] = Query(default=None, description="Last day (UTC), YYYY-MM-DD; default today"),
    days: int = Query(default=30, ge=1, le=app_traffic.MAX_DAYS),
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("apps.view")),
):
    """Bytes the app sent to the public internet per day: `internet_bytes` (its own requests and the answers to
    them), `visitor_bytes` (what its visitors got through nginx) and `total_bytes`."""
    await _get_or_404(app_id, db)
    try:
        start, end = app_traffic.period(start, end, days)
    except ValueError as e:
        raise HTTPException(400, str(e))
    rows = await app_traffic.app_days(db, app_id, start, end)
    total = {k: sum(r[k] for r in rows) for k in ("internet_bytes", "visitor_bytes", "total_bytes")}
    return {"app_id": app_id, "start": start.isoformat(), "end": end.isoformat(), **total, "days": rows}
