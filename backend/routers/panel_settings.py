import asyncio
import logging
import os
from typing import Optional
from fastapi import UploadFile, Depends, Body, HTTPException, File, APIRouter
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import get_db, AsyncSessionLocal
from core.models import Application
from core import auth, token_vault, config as _cfg, system_config as _syscfg
from nginx import certificates as nginx_certificates
from nginx import paths as nginx_paths
from nginx import pages as nginx_pages
from nginx import files as nginx_files
from nginx import config as nginx_config
from nodes.registry import ensure_local_node as _eln
from apps.nginx_sync import _write_app_nginx_config as _wanc
from core.runtime import PORT
from mesh import addressing
from nginx.panel import _load_panel_https_domain, _refresh_all_app_nginx

log = logging.getLogger("cloudbase.main")


router = APIRouter()


# ── System endpoints ──────────────────────────────────────────────────────────
class CloudbaseNginxRequest(BaseModel):
    domain: str
    ssl_cert_path: Optional[str] = None
    ssl_key_path: Optional[str] = None
    base_domain: Optional[str] = None
    base_ssl_cert_path: Optional[str] = None
    base_ssl_key_path: Optional[str] = None


@router.get("/api/system/nginx-config")
async def get_cloudbase_nginx(db: AsyncSession = Depends(get_db), _: dict = Depends(auth.require_permission("system.manage"))):
    config_path = os.path.join(nginx_paths.NGINX_SITES_DIR, "cloudbase")
    exists = os.path.exists(config_path)
    content = None
    if exists:
        with open(config_path) as f:
            content = f.read()
    settings = await _syscfg.get_base_settings(db)
    return {
        "exists": exists,
        "content": content,
        "path": config_path,
        "base_domain": settings["base_domain"],
        "base_ssl_cert_path": settings["base_ssl_cert_path"],
        "base_ssl_key_path": settings["base_ssl_key_path"],
    }


@router.post("/api/system/nginx-config")
async def apply_cloudbase_nginx(
    req: CloudbaseNginxRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(auth.require_permission("system.manage")),
):
    import re as _re2
    # --- Cloudbase panel nginx ---
    unavailable_html = nginx_pages.generate_cloudbase_unavailable_html(req.domain)
    page_ok, page_msg = await asyncio.to_thread(nginx_files.write_cloudbase_unavailable_page, unavailable_html)
    if not page_ok:
        raise HTTPException(status_code=500, detail=f"Failed to write Cloudbase unavailable page: {page_msg}")

    unknown_host_html = nginx_pages.generate_cloudbase_unknown_host_html(req.domain)
    host_ok, host_msg = await asyncio.to_thread(nginx_files.write_cloudbase_unknown_host_page, unknown_host_html)
    if not host_ok:
        raise HTTPException(status_code=500, detail=f"Failed to write Cloudbase unknown-host page: {host_msg}")

    config = nginx_config.generate_config(
        "cloudbase",
        req.domain,
        PORT,
        req.ssl_cert_path,
        req.ssl_key_path,
        strict_hostnames=True,
    )
    ok, msg = await asyncio.to_thread(nginx_files.write_nginx_config, "cloudbase", config)
    if not ok:
        raise HTTPException(status_code=500, detail=f"Failed to apply nginx config: {msg}")

    catchall_ok, catchall_msg = await asyncio.to_thread(nginx_files.write_default_catch_all, req.ssl_cert_path, req.ssl_key_path)
    if not catchall_ok:
        raise HTTPException(status_code=500, detail=f"Cloudbase config applied, but failed to enforce nginx default catch-all: {catchall_msg}")

    # --- Base domain for app subdomains ---
    base_domain = str(req.base_domain or "").strip().strip(".")
    if base_domain and not _re2.match(r'^[a-zA-Z0-9._-]+$', base_domain):
        raise HTTPException(400, "Invalid base_domain")
    base_ssl_cert = str(req.base_ssl_cert_path or "").strip() or None
    base_ssl_key  = str(req.base_ssl_key_path  or "").strip() or None
    await _syscfg.set_base_settings(
        db,
        base_domain=base_domain,
        base_ssl_cert=base_ssl_cert or "",
        base_ssl_key=base_ssl_key or "",
    )

    # Regenerate nginx for all apps in background
    async def _refresh_apps():
        try:
            async with AsyncSessionLocal() as _db:
                _apps = (await _db.execute(select(Application))).scalars().all()
                _local = await _eln(_db)
                _bdom  = _syscfg.get_base_domain_cached()
                for _a in _apps:
                    _has_c = bool(_a.nginx_enabled and _a.domain)
                    if not _has_c and not _bdom:
                        continue
                    try:
                        await _wanc(_a, _db, _local)
                    except Exception as _e:
                        log.warning("nginx refresh failed for app %s: %s", _a.name, _e)
        except Exception as _e:
            log.warning("nginx refresh batch failed: %s", _e)

    asyncio.create_task(_refresh_apps())
    _load_panel_https_domain()
    return {
        "ok": ok,
        "message": msg,
        "preview": config,
        "default_catchall": {"ok": catchall_ok, "message": catchall_msg},
    }


class PanelDomainRequest(BaseModel):
    domain: str
    ssl_cert_path: Optional[str] = None
    ssl_key_path: Optional[str] = None


@router.post("/api/system/panel-domain")
async def set_panel_domain(req: PanelDomainRequest, _: dict = Depends(auth.require_permission("system.manage"))):
    """Set the panel's domain (and certificate) without touching the base domain."""
    domain = nginx_paths._normalize_domain(req.domain)
    if not domain:
        raise HTTPException(400, "Enter a domain")
    cert = nginx_paths._sanitize_ssl_path(req.ssl_cert_path)
    key = nginx_paths._sanitize_ssl_path(req.ssl_key_path)
    for writer, html in (
        (nginx_files.write_cloudbase_unavailable_page, nginx_pages.generate_cloudbase_unavailable_html(domain)),
        (nginx_files.write_cloudbase_unknown_host_page, nginx_pages.generate_cloudbase_unknown_host_html(domain)),
    ):
        ok, msg = await asyncio.to_thread(writer, html)
        if not ok:
            raise HTTPException(500, f"Failed to write status page: {msg}")
    config = nginx_config.generate_config("cloudbase", domain, PORT, cert, key, strict_hostnames=True)
    ok, msg = await asyncio.to_thread(nginx_files.write_nginx_config, "cloudbase", config)
    if not ok:
        raise HTTPException(500, f"Failed to apply nginx config: {msg}")
    await asyncio.to_thread(nginx_files.write_default_catch_all, cert, key)
    _load_panel_https_domain()
    await nginx_certificates.forget_pending([domain])
    return {"ok": True, "domain": domain, "https": bool(cert and key)}


class BaseDomainRequest(BaseModel):
    base_domain: Optional[str] = None


@router.post("/api/system/base-domain")
async def set_base_domain(req: BaseDomainRequest, db: AsyncSession = Depends(get_db), _: dict = Depends(auth.require_permission("system.manage"))):
    """Set (or clear) the base domain for app subdomains. HTTPS is automatic:
    every app gets its own certificate, so no wildcard certificate is kept."""
    import re as _re2
    base_domain = str(req.base_domain or "").strip().strip(".").lower()
    if base_domain and not _re2.match(r'^[a-z0-9.-]+$', base_domain):
        raise HTTPException(400, "Invalid base domain")
    await _syscfg.set_base_settings(db, base_domain=base_domain, base_ssl_cert="", base_ssl_key="")
    asyncio.create_task(_refresh_all_app_nginx())
    return {"ok": True, "base_domain": base_domain}


# ── System settings (ports, limits from config.yaml) ─────────────────────────
@router.get("/api/system/settings", include_in_schema=False)
async def get_system_settings(_: dict = Depends(auth.require_permission("system.manage"))):
    return {
        "auth": {
            "token_expire_seconds": _cfg.get_auth("token_expire_seconds"),
        },
        "ports": {
            "instance_min": _cfg.get_port("instance_min"),
            "instance_max": _cfg.get_port("instance_max"),
        },
        "wireguard": {"endpoint": _cfg.get_wireguard().get("endpoint") or ""},
        "limits": {
            "max_apps":                _cfg.get_limit("max_apps"),
            "max_instances":           _cfg.get_limit("max_instances"),
            "max_nodes":               _cfg.get_limit("max_nodes"),
            "max_restarts_per_window": _cfg.get_limit("max_restarts_per_window"),
            "restart_window_seconds":  _cfg.get_limit("restart_window_seconds"),
        },
    }


@router.post("/api/system/settings", include_in_schema=False)
async def save_system_settings(
    body: dict = Body(...),
    _: dict = Depends(auth.require_permission("system.manage")),
):
    def _int(val, lo, hi, default):
        try:
            v = int(val)
            if lo <= v <= hi:
                return v
        except (TypeError, ValueError):
            pass
        return default

    auth_cfg = body.get("auth",   {})
    ports    = body.get("ports",  {})
    limits   = body.get("limits", {})
    wireguard = body.get("wireguard", {})
    sections = {}
    if auth_cfg:
        sections["auth"] = {
            "token_expire_seconds": _int(auth_cfg.get("token_expire_seconds"), 60, 2592000, _cfg.get_auth("token_expire_seconds")),
        }
    if ports:
        sections["ports"] = {
            "instance_min": _int(ports.get("instance_min"), 1024, 65000, _cfg.get_port("instance_min")),
            "instance_max": _int(ports.get("instance_max"), 1024, 65000, _cfg.get_port("instance_max")),
        }
    if limits:
        sections["limits"] = {
            "max_apps":                _int(limits.get("max_apps"),                1, 100000, _cfg.get_limit("max_apps")),
            "max_instances":           _int(limits.get("max_instances"),           1, 100000, _cfg.get_limit("max_instances")),
            "max_nodes":               _int(limits.get("max_nodes"),               1, 10000,  _cfg.get_limit("max_nodes")),
            "max_restarts_per_window": _int(limits.get("max_restarts_per_window"), 1, 1000,   _cfg.get_limit("max_restarts_per_window")),
            "restart_window_seconds":  _int(limits.get("restart_window_seconds"),  10, 86400, _cfg.get_limit("restart_window_seconds")),
        }
    if wireguard:
        endpoint = str(wireguard.get("endpoint") or "").strip()
        if endpoint and not (addressing.is_valid_endpoint(endpoint if ":" in endpoint else endpoint + ":51820")):
            raise HTTPException(400, "The node network address must be a host name or IP address, optionally with a port")
        sections["wireguard"] = {"endpoint": endpoint}
    if sections:
        _cfg.save_config_sections(sections)
    return {"ok": True}


@router.post("/api/system/nginx-default-catchall")
async def apply_nginx_default_catchall(_: dict = Depends(auth.require_permission("system.manage"))):
    ok, msg = await asyncio.to_thread(nginx_files.write_default_catch_all)
    return {"ok": ok, "message": msg}


@router.post("/api/system/certs/upload")
async def upload_system_cert(file: UploadFile = File(...), _: dict = Depends(auth.require_permission("system.manage"))):
    allowed_exts = {".pem", ".crt", ".cer", ".key"}
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in allowed_exts:
        raise HTTPException(400, "Only .pem, .crt, .cer, .key files are allowed")
    safe_name = os.path.basename(file.filename or "cert").replace("..", "").lstrip("/")
    dest_dir = os.path.expanduser("~/.cloudbase/certs")
    os.makedirs(dest_dir, exist_ok=True)
    if not safe_name or safe_name.startswith("."):
        raise HTTPException(400, "Invalid file name")
    dest_path = os.path.join(dest_dir, safe_name)
    with open(dest_path, "wb") as f:
        f.write(await file.read())
    os.chmod(dest_path, 0o600)  # may be a private key
    return {"path": dest_path}


# ── GitHub token vault ────────────────────────────────────────────────────────
class SaveTokenRequest(BaseModel):
    label: str
    token: str


@router.get("/api/system/github-tokens")
async def list_github_tokens(_: dict = Depends(auth.require_permission("tokens.manage"))):
    return token_vault.list_hints()


@router.post("/api/system/github-tokens")
async def save_github_token(req: SaveTokenRequest, _: dict = Depends(auth.require_permission("tokens.manage"))):
    if not req.label.strip():
        raise HTTPException(400, "Label is required")
    if not req.token.strip():
        raise HTTPException(400, "Token is required")
    token_vault.add(req.label.strip(), req.token.strip())
    return {"ok": True}


@router.delete("/api/system/github-tokens/{token_id}")
async def delete_github_token(token_id: str, _: dict = Depends(auth.require_permission("tokens.manage"))):
    token_vault.remove(token_id)
    return {"ok": True}
