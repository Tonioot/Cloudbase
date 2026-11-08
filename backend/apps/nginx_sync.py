import asyncio
import json
import logging
import math
import os
import re
import socket
from typing import Optional
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import AsyncSessionLocal
from core.models import ApplicationReplica, Node, Application
from containers import processes as pm
from nginx import certificates as nginx_certificates
from nginx import certbot as nginx_certbot
from nginx import config as nginx_config
from nginx import files as nginx_files
from nginx import pages as nginx_pages
from apps import health as app_health
from mesh import routing
from nodes.registry import ensure_local_node
from core import system_config as _syscfg
from apps.state import RESTART_READY_MIN_HEALTHY_RATIO, RESTART_READY_POLL_SECONDS, RESTART_READY_TIMEOUT_SECONDS, TRANSITION_HOLD_MAX_SECONDS, _clear_active_transition_mode, _get_active_transition_mode

log = logging.getLogger("cloudbase.apps")


def _best_effort_remove_app_nginx(app_name: str) -> None:
    """Remove app nginx config even when DB flags are stale, to avoid ghost routes."""
    try:
        nginx_files.remove_nginx_config(app_name)
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


async def _write_app_nginx_config(
    app: Application,
    db: AsyncSession,
    local_node: Node,
    *,
    mode: Optional[str] = None,
) -> None:
    if app.no_web:
        app.nginx_enabled = False
        _best_effort_remove_app_nginx(app.name)
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
    backends = await _get_nginx_backends(app, db, local_node)
    effective_mode = mode or _get_nginx_mode(app)
    if mode is None and effective_mode == "normal":
        active_mode = _get_active_transition_mode(app.id)
        if active_mode in ("restart", "starting"):
            log.info(
                "[nginx-transition-guard] app_id=%s forcing mode=%s (normal write suppressed while transition active)",
                app.id,
                active_mode,
            )
            effective_mode = active_mode
    log.info(
        "[nginx-debug] write app_id=%s app=%s mode=%s backends=%d values=%s has_custom=%s base_domain=%s",
        app.id,
        app.name,
        effective_mode,
        len(backends),
        backends,
        has_custom,
        bool(_base),
    )
    config = nginx_config.generate_config(
        app.name,
        app.domain if has_custom else None,
        backends,
        ssl_cert,
        ssl_key,
        app_id=app.id,
        mode=effective_mode,
        extra_domains=json.loads(app.extra_domains or "[]") if has_custom else [],
        redirect_domains=json.loads(app.redirect_domains or "[]") if has_custom else [],
    )
    ok, msg = await asyncio.to_thread(nginx_files.write_nginx_config, app.name, config)
    if not ok:
        raise HTTPException(500, f"Nginx config failed: {msg}")

    if not has_custom and not _syscfg.get_base_ssl_cert_cached():
        _schedule_auto_certificate(app)


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
                await _write_app_nginx_config(fresh, _db, await ensure_local_node(_db))

    asyncio.create_task(nginx_certificates.ensure_auto_certificate(name, _rewrite))


async def _wait_for_app_backends_ready(
    app_id: int,
    timeout_seconds: int = RESTART_READY_TIMEOUT_SECONDS,
) -> tuple[bool, str]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last_reason = "no running instance backends yet"
    stable_ready_polls = 0

    while asyncio.get_running_loop().time() < deadline:
        async with AsyncSessionLocal() as db:
            app = await db.get(Application, app_id)
            if not app:
                return False, "app was removed before nginx restore"
            backends = await _get_nginx_backends(app, db)

        if not backends:
            last_reason = "no running instance backends yet"
            await asyncio.sleep(RESTART_READY_POLL_SECONDS)
            continue

        ready = 0
        checked = 0
        health_path, health_expect, _ = app_health.settings_of(app)
        for backend in backends:
            try:
                host, port = routing.split(str(backend))
            except ValueError:
                continue
            checked += 1
            if await asyncio.to_thread(_http_service_ready, host, port, health_path, health_expect):
                ready += 1

        # Require a healthy quorum for two consecutive polls so restart pages
        # are not cleared prematurely while still allowing partial recovery.
        required = max(1, math.ceil(checked * RESTART_READY_MIN_HEALTHY_RATIO)) if checked else 0
        if checked and ready >= required:
            stable_ready_polls += 1
            if stable_ready_polls >= 2:
                return True, f"{ready}/{checked} backend(s) healthy (threshold {required})"
            last_reason = f"{ready}/{checked} backend(s) healthy (threshold {required}), waiting for stability"
            await asyncio.sleep(RESTART_READY_POLL_SECONDS)
            continue

        stable_ready_polls = 0

        last_reason = f"{ready}/{checked} backend(s) healthy (threshold {required})"
        await asyncio.sleep(RESTART_READY_POLL_SECONDS)

    return False, last_reason


async def _restore_nginx_after_transition(
    app_id: int,
    started_at: float,
    transition_label: str,
) -> None:
    ready, reason = await _wait_for_app_backends_ready(app_id)
    elapsed = max(asyncio.get_running_loop().time() - started_at, 0)

    # Do not clear start/restart pages while backends are still unhealthy.
    # Keep waiting in the background, with a hard cap to avoid infinite loops.
    if not ready and elapsed < TRANSITION_HOLD_MAX_SECONDS:
        log.info(
            "[nginx-transition-debug] app_id=%s label=%s keep_transition_page elapsed=%.1fs reason=%s",
            app_id,
            transition_label,
            elapsed,
            reason,
        )
        pm._push_line(
            app_id,
            f"{transition_label.capitalize()} page still active after {elapsed:.1f}s ({reason}); waiting for healthy backends...",
        )
        asyncio.create_task(_restore_nginx_after_transition(app_id, started_at, transition_label))
        return

    # Transition window has ended (ready or hard timeout): allow normal writes again.
    _clear_active_transition_mode(app_id)

    async with AsyncSessionLocal() as db:
        app = await db.get(Application, app_id)
        _base = _syscfg.get_base_domain_cached()
        has_custom = bool(app and app.nginx_enabled and app.domain)
        if not app or (not has_custom and not _base):
            return

        local_node = await ensure_local_node(db)
        backends = await _get_nginx_backends(app, db, local_node)
        if not backends:
            # Readiness can race with status transitions; keep the transition page
            # active until backends are still present when we restore nginx.
            if elapsed < TRANSITION_HOLD_MAX_SECONDS:
                log.info(
                    "[nginx-transition-debug] app_id=%s label=%s restore_race_no_backends elapsed=%.1fs",
                    app_id,
                    transition_label,
                    elapsed,
                )
                pm._push_line(
                    app_id,
                    f"{transition_label.capitalize()} page still active after {elapsed:.1f}s (backends temporarily unavailable); retrying...",
                )
                asyncio.create_task(_restore_nginx_after_transition(app_id, started_at, transition_label))
                return
            pm._push_line(
                app_id,
                f"{transition_label.capitalize()} page kept active after {elapsed:.1f}s; no running backends available to restore normal nginx routing.",
            )
            return

        ssl_cert = ssl_key = None
        if has_custom:
            ssl_cert, ssl_key = await asyncio.to_thread(_resolve_ssl_paths, app.ssl_cert_path, app.ssl_key_path)
        effective_mode = _get_nginx_mode(app)
        log.info(
            "[nginx-transition-debug] app_id=%s label=%s restoring mode=%s backends=%d values=%s",
            app_id,
            transition_label,
            effective_mode,
            len(backends),
            backends,
        )
        config = nginx_config.generate_config(
            app.name,
            app.domain if has_custom else None,
            backends,
            ssl_cert,
            ssl_key,
            app_id=app.id,
            mode=effective_mode,
            extra_domains=json.loads(app.extra_domains or "[]") if has_custom else [],
            redirect_domains=json.loads(app.redirect_domains or "[]") if has_custom else [],
        )
        ok, msg = await asyncio.to_thread(nginx_files.write_nginx_config, app.name, config)

    log.info(
        "[nginx-transition] app_id=%d label=%s ready=%s elapsed=%.1fs reason=%r nginx_ok=%s msg=%r",
        app_id,
        transition_label,
        ready,
        elapsed,
        reason,
        ok,
        msg,
    )
    if ok:
        status_text = "cleared" if ready else "force-cleared after hard timeout"
        pm._push_line(
            app_id,
            f"{transition_label.capitalize()} page {status_text} after {elapsed:.1f}s ({reason}).",
        )
    else:
        pm._push_line(app_id, f"Failed to restore nginx after {transition_label}: {msg}")


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
    slug = re.sub(r"[^a-z0-9]+", "-", (app.name or "").lower()).strip("-")
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


def _local_port_accepts_connections(port: int) -> bool:
    """Return True when localhost:port accepts TCP connections."""
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.5):
            return True
    except Exception:
        return False


def _http_service_ready(host: str, port: int, path: str = "/", expect: str = "") -> bool:
    """Return True when host:port answers the way the app's health check says is healthy."""
    if host == "127.0.0.1" and not _local_port_accepts_connections(port):
        return False
    return app_health.answer(host, int(port), path, expect)[0]


async def _get_nginx_backends(app: Application, db: AsyncSession, local_node: "Node" = None) -> "list[str]":
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
