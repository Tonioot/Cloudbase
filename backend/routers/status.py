"""Uptime: the public status page (only apps that opted in) and the uptime of one app for the panel."""
import time

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps import uptime as apps_uptime
from apps.serializers import _get_or_404
from core import auth as _auth
from core.database import AsyncSessionLocal, get_db
from core.models import Application, Instance

router = APIRouter(tags=["status"])

_CACHE_SECONDS = 15           # the page is public: many visitors must not mean many queries
_cache: tuple[float, dict] = (0.0, {})


def overall_state(states: list[str]) -> str:
    """One word for the whole page. "Operational" needs something that really runs and answers; apps that were
    stopped on purpose do not spoil it, but when nothing runs the page does not claim that all is well."""
    if not states:
        return "none"
    for state in ("down", "maintenance", "up", "unknown"):
        if state in states:
            return state
    return "stopped"


async def _running_app_ids(db) -> set[int]:
    return set((await db.execute(select(Instance.app_id).where(Instance.status == "running"))).scalars())


@router.get("/api/status")
async def public_status():
    """No sign-in. Shows only the apps that have "Show on the Cloudbase status page" switched on, by name."""
    global _cache
    now = time.monotonic()
    if _cache[1] and now - _cache[0] < _CACHE_SECONDS:
        return _cache[1]
    async with AsyncSessionLocal() as db:
        running = await _running_app_ids(db)
        apps = (await db.execute(select(Application).where(Application.show_on_status_page == True)  # noqa: E712
                                 .order_by(Application.name))).scalars().all()
        items = []
        for app in apps:
            data = await apps_uptime.summary(db, app.id, needed=app.uptime_failures or apps_uptime.FAILURES_FOR_OUTAGE)
            items.append({"name": app.name, "status": apps_uptime.state_of(app, app.id in running, data["last_check"]), **data})
    result = {"overall": overall_state([i["status"] for i in items]), "apps": items}
    _cache = (now, result)
    return result


@router.get("/api/apps/{app_id}/uptime")
async def app_uptime(app_id: int, db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("apps.view"))):
    app = await _get_or_404(app_id, db)
    running = await _running_app_ids(db)
    data = await apps_uptime.summary(db, app_id, needed=app.uptime_failures or apps_uptime.FAILURES_FOR_OUTAGE)
    return {"status": apps_uptime.state_of(app, app_id in running, data["last_check"]), **data}
