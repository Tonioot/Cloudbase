from fastapi import APIRouter, Request, Query, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import get_db
import datetime as _dt
from core.models import StatsHistory, Application, ApplicationReplica
from nodes.registry import ensure_local_node
from core import auth as _auth
from apps.runtime import _load_node_map
from apps.serializers import _get_or_404, _replica_to_dict, _app_to_dict
from routers.applications.deps import _request_can_manage_apps

router = APIRouter(prefix="/api/apps", tags=["applications"])


@router.get("")
async def list_apps(request: Request, db: AsyncSession = Depends(get_db), _viewer: dict = Depends(_auth.require_permission("apps.view"))):
    await ensure_local_node(db)
    result = await db.execute(select(Application))
    apps = result.scalars().all()
    node_map = await _load_node_map(db)

    # Batch-fetch all replica rows so we can derive statuses without N+1 Docker checks
    rep_result = await db.execute(select(ApplicationReplica))
    all_replicas = rep_result.scalars().all()
    replica_map: dict[int, list] = {}
    for r in all_replicas:
        replica_map.setdefault(r.app_id, []).append(r)

    include_sensitive = await _request_can_manage_apps(request)
    result_list = []
    for a in apps:
        app_replicas = replica_map.get(a.id, [])
        replica_dicts = [_replica_to_dict(r, node_map.get(r.node_id)) for r in app_replicas]
        result_list.append(
            _app_to_dict(
                a,
                include_sensitive=include_sensitive,
                replicas=replica_dicts,
                include_page_configs=False,
            )
        )
    return result_list


@router.get("/{app_id}")
async def get_app(app_id: int, request: Request, _user: dict = Depends(_auth.require_permission("apps.view")), db: AsyncSession = Depends(get_db)):
    await ensure_local_node(db)
    app = await _get_or_404(app_id, db)
    node_map = await _load_node_map(db)
    replica_result = await db.execute(
        select(ApplicationReplica).where(ApplicationReplica.app_id == app_id)
    )
    replicas_dicts = [_replica_to_dict(r, node_map.get(r.node_id)) for r in replica_result.scalars().all()]
    return _app_to_dict(app, replicas_dicts, include_sensitive=await _request_can_manage_apps(request))


@router.get("/{app_id}/stats/history")
async def get_stats_history(
    app_id: int,
    hours: int = Query(24, ge=1, le=168),
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("apps.view")),
):
    since = _dt.datetime.utcnow() - _dt.timedelta(hours=hours)
    result = await db.execute(
        select(StatsHistory)
        .where(StatsHistory.app_id == app_id, StatsHistory.timestamp >= since)
        .order_by(StatsHistory.timestamp.asc())
    )
    rows = result.scalars().all()
    return [
        {
            "timestamp":   r.timestamp.isoformat() + "Z",
            "cpu_percent": r.cpu_percent,
            "memory_mb":   r.memory_mb,
            "net_mb":      r.net_mb  or 0,
            "disk_mb":     r.disk_mb or 0,
        }
        for r in rows
    ]
