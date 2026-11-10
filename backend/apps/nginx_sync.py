import asyncio
import json
import logging
import os
from typing import Optional
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import AsyncSessionLocal
from core.models import ApplicationReplica, Node, Application
from nginx import certificates as nginx_certificates
from nginx import paths as nginx_paths
from nginx import certbot as nginx_certbot
from nginx import config as nginx_config
from nginx import files as nginx_files
from nginx import pages as nginx_pages
from mesh import routing
from core import system_config as _syscfg

log = logging.getLogger("cloudbase.apps")


def _best_effort_remove_app_nginx(app_id: int) -> None:
    """Remove app nginx config even when DB flags are stale, to avoid ghost routes."""
    try:
        nginx_files.remove_nginx_config(nginx_paths.app_key(app_id))
    except Exception:
        pass


def _get_nginx_mode(app: Application) -> str:
    if app.update_mode:
        return "update"
    if app.maintenance_mode:
        return "maintenance"
    return "normal"


def _has_public_nginx_domain(app: Application) -> bool:
    """True when app traffic can be routed via custom or auto-subdomain nginx."""
    if app.no_web:
        return False
    return bool((app.nginx_enabled and app.domain) or _syscfg.get_base_domain_cached())


async def _write_app_nginx_config(app: Application, db: AsyncSession) -> None:
    """Write the app's nginx config from what is true now: its domains, its running instances and its mode."""
    if app.no_web:
        app.nginx_enabled = False
        _best_effort_remove_app_nginx(app.id)
        return

    _base = _syscfg.get_base_domain_cached()
    has_custom = bool(app.nginx_enabled and app.domain)
    if not has_custom and not _base:
        return

    maint_ok, maint_msg = await asyncio.to_thread(_ensure_maintenance_files, app, app.id)
    if not maint_ok:
        raise HTTPException(500, f"Maintenance files failed: {maint_msg}")

    ssl_cert = ssl_key = None
    if has_custom:
        ssl_cert, ssl_key = await asyncio.to_thread(_resolve_ssl_paths, app.ssl_cert_path, app.ssl_key_path)
    backends = await _get_nginx_backends(app, db)
    mode = await _nginx_mode(app, db, backends)
    log.debug("[nginx] write app_id=%s mode=%s backends=%s", app.id, mode, backends)
    config = nginx_config.generate_config(
        app.name,
        app.domain if has_custom else None,
        backends,
        ssl_cert,
        ssl_key,
        app_id=app.id,
        mode=mode,
        extra_domains=json.loads(app.extra_domains or "[]") if has_custom else [],
        redirect_domains=json.loads(app.redirect_domains or "[]") if has_custom else [],
    )
    ok, msg = await asyncio.to_thread(nginx_files.write_nginx_config, nginx_paths.app_key(app.id), config)
    if not ok:
        raise HTTPException(500, f"Nginx config failed: {msg}")

    if not has_custom and not _syscfg.get_base_ssl_cert_cached():
        _schedule_auto_certificate(app)


# Instances on their way up: while none of the app's instances is live yet, visitors see the start or restart page
_COMING_UP = ("pending", "starting")


async def _nginx_mode(app: Application, db: AsyncSession, backends: list[str]) -> str:
    """What nginx shows. Follows from the app and its instances, so every write agrees and nothing has to be undone later.

    update / maintenance mode first; then the app itself as soon as one instance is live; while instances are still
    coming up the restart page (they ran before: they have a container) or the start page; otherwise 'normal', which
    shows the downtime page when there is nothing to send visitors to."""
    mode = _get_nginx_mode(app)
    if mode != "normal" or backends:
        return mode
    coming_up = (await db.execute(
        select(ApplicationReplica.container_id).where(
            ApplicationReplica.app_id == app.id,
            ApplicationReplica.desired == "running",
            ApplicationReplica.status.in_(_COMING_UP),
        )
    )).scalars().all()
    if not coming_up:
        return "normal"
    return "restart" if any(coming_up) else "starting"


async def refresh_app_nginx(app_id: int) -> None:
    """Rewrite one app's nginx config after its instances changed (one went live, failed or dropped out)."""
    async with AsyncSessionLocal() as db:
        app = await db.get(Application, app_id)
        if not app or not _has_public_nginx_domain(app):
            return
        try:
            await _write_app_nginx_config(app, db)
        except Exception as e:
            log.warning("[nginx] rewriting the config of app %d failed: %s", app_id, e)


def _schedule_auto_certificate(app: Application) -> None:
    """Apps on the base domain get a certificate for their own subdomain.
    Once it's there, the nginx config is written again with HTTPS."""
    name = _auto_subdomain(app)
    if not name or not nginx_certificates.wants_auto_certificate(name):
        return
    app_id = app.id

    async def _rewrite():
        async with AsyncSessionLocal() as _db:
            fresh = await _db.get(Application, app_id)
            if fresh:
                await _write_app_nginx_config(fresh, _db)

    asyncio.create_task(nginx_certificates.ensure_auto_certificate(name, _rewrite))


def _best_effort_delete_app_certificate(app: Application) -> None:
    """Remove the app's Let's Encrypt certificates (its own domains and its
    automatic subdomain), so certbot stops renewing them — renewals would
    fail once the domains point elsewhere."""
    names = {nginx_certbot.name_from_path(app.ssl_cert_path), _auto_subdomain(app)}
    for name in filter(None, names):
        nginx_certbot.delete(name)


def _auto_subdomain(app: Application) -> Optional[str]:
    """<app>.<base domain> — mirrors nginx_manager.generate_config."""
    base = _syscfg.get_base_domain_cached()
    slug = nginx_paths.subdomain_slug(app.name)
    return f"{slug}.{base}" if base and slug else None


def _resolve_ssl_paths(cert: str | None, key: str | None) -> tuple[str | None, str | None]:
    """Return cert/key paths only if both files actually exist on disk; otherwise None."""
    if cert and key:
        name = nginx_certbot.name_from_path(cert)
        if name and nginx_certbot.name_from_path(key) == name:
            # /etc/letsencrypt is root-only; the helper checks it for us
            if nginx_certbot.exists(name):
                return cert, key
            log.warning("Let's Encrypt certificate %s not found, skipping SSL", name)
            return None, None
    if cert and key and os.path.isfile(cert) and os.path.isfile(key):
        return cert, key
    if cert or key:
        missing = [p for p in (cert, key) if p and not os.path.isfile(p)]
        log.warning("SSL cert/key file(s) not found on disk, skipping SSL: %s", missing)
    return None, None


def _ensure_maintenance_files(app: Application, app_id: int) -> tuple[bool, str]:
    """Write maintenance HTML files from stored config (or defaults)."""
    downtime_cfg = json.loads(app.downtime_page  or "{}")
    update_cfg   = json.loads(app.update_page    or "{}")
    restart_cfg  = json.loads(app.restart_page   or "{}")
    starting_cfg = json.loads(app.starting_page  or "{}")

    def _page_meta(cfg: dict) -> dict:
        logo = cfg.get("logo_data")
        html = cfg.get("custom_html")
        return {
            "has_logo": bool(logo),
            "logo_len": len(logo) if isinstance(logo, str) else 0,
            "has_custom_html": bool(html),
            "custom_html_len": len(html) if isinstance(html, str) else 0,
            "has_status_url": bool(cfg.get("status_url")),
        }

    log.debug(
        "[ensure-files] app_id=%d page_meta downtime=%s update=%s restart=%s starting=%s",
        app_id,
        _page_meta(downtime_cfg),
        _page_meta(update_cfg),
        _page_meta(restart_cfg),
        _page_meta(starting_cfg),
    )

    downtime_html = nginx_pages.render_app_page("downtime", downtime_cfg)
    update_html = nginx_pages.render_app_page("update", update_cfg)
    restart_html = nginx_pages.render_app_page("restart", restart_cfg)
    starting_html = nginx_pages.render_app_page("starting", starting_cfg)
    ok, msg = nginx_files.write_maintenance_files(app_id, downtime_html, update_html, restart_html, starting_html)
    log.log(logging.DEBUG if ok else logging.WARNING, "[ensure-files] write result ok=%s msg=%r", ok, msg)
    return ok, msg


async def _get_nginx_backends(app: Application, db: AsyncSession) -> list[str]:
    """Return nginx backend addresses for an app as a list of 'host:port' strings.

    Local replicas use 127.0.0.1:{external_port}.
    Remote replicas use {node wg_ip}:{external_port} (over the node network).
    Replicas that are running but have no address yet are omitted.
    Returns an empty list when no running replicas exist.
    """
    result = await db.execute(
        select(ApplicationReplica, Node)
        .join(Node, ApplicationReplica.node_id == Node.id, isouter=True)
        .where(
            ApplicationReplica.app_id == app.id,
            ApplicationReplica.status == "running",
        )
    )
    backends: list[str] = []
    for replica, r_node in result.all():
        backend = routing.backend_for(replica, r_node)
        if backend:
            backends.append(backend)
    return backends
