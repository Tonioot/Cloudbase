from fastapi import Depends, APIRouter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import get_db
from core import auth
from core.models import NodeMetricsHistory as _NMH, OverviewHistory as _OH
from core.history import _HISTORY_RETENTION_DAYS, _OVERVIEW_SAMPLE_SECONDS, _downsample, _ts_ms
from core.runtime import PORT, VERSION
from nodes import metrics as node_metrics

router = APIRouter()


@router.get("/api/nodes/{node_id}/metrics/history")
async def node_metrics_history(node_id: int, hours: float = 1,
                               _user: dict = Depends(auth.require_permission("nodes.view")),
                               db: AsyncSession = Depends(get_db)):
    """Usage, network, disk I/O (rates per second) and load of one node over the last `hours` (oldest first)."""
    import datetime as _dt
    hours = max(0.25, min(hours, 24 * _HISTORY_RETENTION_DAYS))
    since = _dt.datetime.utcnow() - _dt.timedelta(hours=hours)
    res = await db.execute(
        select(_NMH).where(_NMH.node_id == node_id, _NMH.timestamp >= since).order_by(_NMH.timestamp.asc())
    )
    rows = node_metrics.chart_samples(res.scalars().all(), _ts_ms)
    return {"interval_seconds": _OVERVIEW_SAMPLE_SECONDS, "hours": hours,
            "samples": _downsample(rows, node_metrics.CHART_KEYS)}


@router.get("/api/overview/history")
async def overview_history(hours: float = 1,
                           _user: dict = Depends(auth.require_permission("apps.view")),
                           db: AsyncSession = Depends(get_db)):
    """Cluster samples for the dashboard sparklines over the last `hours` (oldest first)."""
    import datetime as _dt
    hours = max(0.25, min(hours, 24 * _HISTORY_RETENTION_DAYS))
    since = _dt.datetime.utcnow() - _dt.timedelta(hours=hours)
    res = await db.execute(select(_OH).where(_OH.timestamp >= since).order_by(_OH.timestamp.asc()))
    keys = ("apps_running", "apps_total", "instances", "cpu", "mem_used_mb", "mem_total_mb", "nodes_online", "nodes_total")
    rows = [{
        "ts": _ts_ms(r.timestamp), "apps_running": r.apps_running, "apps_total": r.apps_total,
        "instances": r.instances, "cpu": r.cpu_percent, "mem_used_mb": r.mem_used_mb,
        "mem_total_mb": r.mem_total_mb, "nodes_online": r.nodes_online, "nodes_total": r.nodes_total,
    } for r in res.scalars().all()]
    return {"interval_seconds": _OVERVIEW_SAMPLE_SECONDS, "hours": hours, "samples": _downsample(rows, keys)}


@router.get("/api/health")
async def health():
    return {"status": "ok", "version": VERSION, "port": PORT}
