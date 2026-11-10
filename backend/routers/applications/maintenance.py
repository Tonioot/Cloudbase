import asyncio
import json
import logging
from fastapi import HTTPException, Depends, APIRouter
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import get_db
from nginx import pages as nginx_pages
from nginx import files as nginx_files
from core import auth as _auth
from apps.nginx_sync import _has_public_nginx_domain, _write_app_nginx_config
from apps.serializers import _app_to_dict, _get_or_404, _instance_views
from routers.applications.schemas import MaintenanceSettings, RenderPageRequest

router = APIRouter(prefix="/api/apps", tags=["applications"])


log = logging.getLogger("cloudbase.apps")


# ── Maintenance page endpoints ─────────────────────────────────────────────

@router.put("/{app_id}/maintenance-pages")
async def save_maintenance_pages(
    app_id: int,
    req: MaintenanceSettings,
    _user: dict = Depends(_auth.require_permission("apps.configure")),
    db: AsyncSession = Depends(get_db),
):
    app = await _get_or_404(app_id, db)

    app.downtime_page = json.dumps(req.downtime_page.model_dump())
    app.update_page   = json.dumps(req.update_page.model_dump())
    app.restart_page  = json.dumps(req.restart_page.model_dump())
    app.starting_page = json.dumps(req.starting_page.model_dump())
    downtime_html = nginx_pages.render_app_page("downtime", req.downtime_page.model_dump())
    update_html = nginx_pages.render_app_page("update", req.update_page.model_dump())
    restart_html = nginx_pages.render_app_page("restart", req.restart_page.model_dump())
    starting_html = nginx_pages.render_app_page("starting", req.starting_page.model_dump())
    ok, msg = await asyncio.to_thread(nginx_files.write_maintenance_files, app_id, downtime_html, update_html, restart_html, starting_html)
    if not ok:
        await db.commit()
        return {"ok": False, "message": msg}

    # Regenerate and reload nginx if configured, so changes take effect immediately
    if _has_public_nginx_domain(app):
        await _write_app_nginx_config(app, db)

    await db.commit()
    return {"ok": True, "message": "Saved"}


@router.post("/{app_id}/maintenance-mode/toggle")
async def toggle_maintenance_mode(app_id: int, _user: dict = Depends(_auth.require_permission("apps.configure")), db: AsyncSession = Depends(get_db)):
    return await _toggle(app_id, db, "maintenance_mode", "update_mode")


@router.post("/{app_id}/update-mode/toggle")
async def toggle_update_mode(app_id: int, _user: dict = Depends(_auth.require_permission("apps.configure")), db: AsyncSession = Depends(get_db)):
    return await _toggle(app_id, db, "update_mode", "maintenance_mode")


async def _toggle(app_id: int, db: AsyncSession, flag: str, other: str) -> dict:
    """Switch maintenance or update mode; only one of the two is on at a time. nginx follows (apps/nginx_sync.py)."""
    app = await _get_or_404(app_id, db)
    if not _has_public_nginx_domain(app):
        what = "maintenance" if flag == "maintenance_mode" else "update"
        raise HTTPException(400, f"Configure a custom app domain or base domain to use {what} mode")
    setattr(app, flag, not getattr(app, flag))
    if getattr(app, flag):
        setattr(app, other, False)
    log.info("[maintenance] app_id=%d %s=%s", app_id, flag, getattr(app, flag))
    await _write_app_nginx_config(app, db)
    await db.commit()
    return _app_to_dict(app, await _instance_views(app.id, db))


@router.post("/{app_id}/maintenance-pages/render")
async def render_maintenance_page(
    app_id: int,
    req: RenderPageRequest,
    _user: dict = Depends(_auth.require_permission("apps.view")),
    db: AsyncSession = Depends(get_db),
):
    """Render unsaved page settings, for the editor's live preview."""
    if req.page_type not in ("downtime", "update", "restart", "starting"):
        raise HTTPException(400, "page_type must be 'downtime', 'update', 'restart', or 'starting'")
    await _get_or_404(app_id, db)
    return HTMLResponse(content=nginx_pages.render_app_page(req.page_type, req.page.model_dump(), preview=True))

