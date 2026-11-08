import asyncio
import json
import logging
from fastapi import HTTPException, Depends, APIRouter
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import get_db
from nginx import pages as nginx_pages
from nginx import files as nginx_files
from nodes.registry import ensure_local_node
from core import auth as _auth
from apps.nginx_sync import _ensure_maintenance_files, _get_nginx_mode, _has_public_nginx_domain, _write_app_nginx_config
from apps.serializers import _app_to_dict, _get_or_404
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
    local_node = await ensure_local_node(db)

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
        mode = _get_nginx_mode(app)
        await _write_app_nginx_config(app, db, local_node, mode=mode)
        nginx_ok, nginx_msg = True, "ok"
        if not nginx_ok:
            await db.commit()
            return {"ok": False, "message": f"Files saved but nginx reload failed: {nginx_msg}"}

    await db.commit()
    return {"ok": True, "message": "Saved"}


@router.post("/{app_id}/maintenance-mode/toggle")
async def toggle_maintenance_mode(app_id: int, _user: dict = Depends(_auth.require_permission("apps.configure")), db: AsyncSession = Depends(get_db)):
    local_node = await ensure_local_node(db)
    app = await _get_or_404(app_id, db)
    
    if not _has_public_nginx_domain(app):
        raise HTTPException(400, "Configure a custom app domain or base domain to use maintenance mode")

    app.maintenance_mode = not (app.maintenance_mode or False)
    if app.maintenance_mode:
        app.update_mode = False  # mutex: only one mode at a time

    mode = _get_nginx_mode(app)
    log.info("[toggle-maintenance] app_id=%d new_mode=%r nginx_mode=%r domain=%r",
             app_id, app.maintenance_mode, mode, app.domain)

    maint_ok, maint_msg = _ensure_maintenance_files(app, app_id)
    if not maint_ok:
        raise HTTPException(500, f"Maintenance files failed: {maint_msg}")
    await _write_app_nginx_config(app, db, local_node, mode=mode)
    ok, msg = True, "ok"
    log.info("[toggle-maintenance] write_nginx_config ok=%s msg=%r", ok, msg)
    if not ok:
        raise HTTPException(500, f"Nginx config failed: {msg}")

    await db.commit()
    return _app_to_dict(app)


@router.post("/{app_id}/update-mode/toggle")
async def toggle_update_mode(app_id: int, _user: dict = Depends(_auth.require_permission("apps.configure")), db: AsyncSession = Depends(get_db)):
    local_node = await ensure_local_node(db)
    app = await _get_or_404(app_id, db)

    if not _has_public_nginx_domain(app):
        raise HTTPException(400, "Configure a custom app domain or base domain to use update mode")

    app.update_mode = not (app.update_mode or False)
    if app.update_mode:
        app.maintenance_mode = False  # mutex: only one mode at a time

    mode = _get_nginx_mode(app)
    log.info("[toggle-update] app_id=%d new_mode=%r nginx_mode=%r domain=%r",
             app_id, app.update_mode, mode, app.domain)

    maint_ok, maint_msg = _ensure_maintenance_files(app, app_id)
    if not maint_ok:
        raise HTTPException(500, f"Maintenance files failed: {maint_msg}")
    await _write_app_nginx_config(app, db, local_node, mode=mode)
    ok, msg = True, "ok"
    log.info("[toggle-update] write_nginx_config ok=%s msg=%r", ok, msg)
    if not ok:
        raise HTTPException(500, f"Nginx config failed: {msg}")

    await db.commit()
    return _app_to_dict(app)


@router.post("/{app_id}/maintenance-pages/render")
async def render_maintenance_page(
    app_id: int,
    req: RenderPageRequest,
    _user: dict = Depends(_auth.require_permission("apps.view")),
    db: AsyncSession = Depends(get_db),
):
    """Render unsaved page settings, for the editor's live preview."""
    from fastapi.responses import HTMLResponse

    if req.page_type not in ("downtime", "update", "restart", "starting"):
        raise HTTPException(400, "page_type must be 'downtime', 'update', 'restart', or 'starting'")
    await _get_or_404(app_id, db)
    return HTMLResponse(content=nginx_pages.render_app_page(req.page_type, req.page.model_dump(), preview=True))

