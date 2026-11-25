import asyncio
import logging
import os
import re
from core.system_settings import clean_system_settings
from typing import Optional
from fastapi import Depends, Body, HTTPException, APIRouter
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import get_db
from core import auth, token_vault, config as _cfg, system_config as _syscfg
from workers import log_settings
from nginx import own_certificate
from nginx import paths as nginx_paths
from nginx import pages as nginx_pages
from nginx import files as nginx_files
from dbs.public import sync_public_access
from nginx.panel import _refresh_all_app_nginx, apply_panel_domain
from nodes import capacity

log = logging.getLogger("cloudbase.main")


router = APIRouter()


# ── System endpoints ──────────────────────────────────────────────────────────
@router.get("/api/system/nginx-config")
async def get_cloudbase_nginx(db: AsyncSession = Depends(get_db), _: dict = Depends(auth.require_permission("system.manage", "system.platform"))):
    config_path = os.path.join(nginx_paths.NGINX_SITES_DIR, "cloudbase")
    exists = os.path.exists(config_path)
    content = None
    if exists:
        with open(config_path) as f:
            content = f.read()
    settings = await _syscfg.get_base_settings(db)
    base_cert = await asyncio.to_thread(own_certificate.inspect_saved, settings["base_ssl_cert_path"])
    return {
        "base_cert": base_cert,         # expiry and names of an uploaded wildcard certificate (None: automatic or set up by hand)
        "exists": exists,
        "content": content,
        "path": config_path,
        "base_domain": settings["base_domain"],
        "base_ssl_cert_path": settings["base_ssl_cert_path"],
        "base_ssl_key_path": settings["base_ssl_key_path"],
        "db_domain": settings["db_domain"],
        "db_cert": await asyncio.to_thread(own_certificate.inspect_saved, settings["db_ssl_cert_path"]),
    }


class PanelDomainRequest(BaseModel):
    domain: str
    ssl_cert_path: Optional[str] = None
    ssl_key_path: Optional[str] = None


@router.post("/api/system/panel-domain")
async def set_panel_domain(req: PanelDomainRequest, _: dict = Depends(auth.require_permission("system.manage", "system.platform"))):
    """Set the panel's domain (and certificate) without touching the base domain."""
    domain = nginx_paths._normalize_domain(req.domain)
    if not domain:
        raise HTTPException(400, "Enter a domain")
    cert = nginx_paths._sanitize_ssl_path(req.ssl_cert_path)
    key = nginx_paths._sanitize_ssl_path(req.ssl_key_path)
    try:
        await apply_panel_domain(domain, cert, key)
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return {"ok": True, "domain": domain, "https": bool(cert and key)}


class BaseDomainRequest(BaseModel):
    base_domain: Optional[str] = None
    # A wildcard certificate of your own (PEM text). Without it HTTPS is automatic: one certificate per app.
    ssl_cert_pem: Optional[str] = None
    ssl_key_pem: Optional[str] = None


@router.post("/api/system/base-domain")
async def set_base_domain(req: BaseDomainRequest, db: AsyncSession = Depends(get_db), _: dict = Depends(auth.require_permission("system.manage", "system.platform"))):
    """Set (or clear) the base domain for app subdomains. Without a certificate HTTPS is automatic: every app gets
    its own. With ssl_cert_pem and ssl_key_pem the user's own wildcard certificate (*.<base domain>) serves every app."""
    import re as _re2
    base_domain = str(req.base_domain or "").strip().strip(".").lower()
    if base_domain and not _re2.match(r'^[a-z0-9.-]+$', base_domain):
        raise HTTPException(400, "Invalid base domain")
    cert_path = key_path = ""
    info = None
    if (req.ssl_cert_pem or "").strip() or (req.ssl_key_pem or "").strip():
        if not base_domain:
            raise HTTPException(400, "Enter the base domain the certificate is for")
        try:
            cert_path, key_path, info = await asyncio.to_thread(
                own_certificate.save, req.ssl_cert_pem or "", req.ssl_key_pem or "", base_domain)
        except own_certificate.CertificateProblem as e:
            raise HTTPException(400, str(e))
    await _syscfg.set_base_settings(db, base_domain=base_domain, base_ssl_cert=cert_path, base_ssl_key=key_path)
    await asyncio.to_thread(own_certificate.forget_except, cert_path or None)
    asyncio.create_task(_refresh_all_app_nginx())
    return {"ok": True, "base_domain": base_domain, "own_certificate": info}


class BrandRequest(BaseModel):
    name: Optional[str] = None
    logo_data: Optional[str] = None             # data:image/...;base64,...
    unknown_host_title: Optional[str] = None
    unknown_host_message: Optional[str] = None


@router.get("/api/system/brand")
async def get_brand(db: AsyncSession = Depends(get_db), _: dict = Depends(auth.require_permission("system.platform", "system.manage"))):
    await _syscfg.load_cache(db)
    return _syscfg.get_brand_cached()


@router.put("/api/system/brand")
async def set_brand(req: BrandRequest, db: AsyncSession = Depends(get_db), _: dict = Depends(auth.require_permission("system.platform", "system.manage"))):
    """White label: the pages visitors can see (an unknown hostname) carry this brand instead of Cloudbase's, and say
    nothing about the server. For a platform built on Cloudbase. Every field empty: Cloudbase's own pages again."""
    if req.logo_data and not re.match(nginx_pages._SAFE_LOGO, req.logo_data):
        raise HTTPException(400, "The logo must be a data:image/...;base64 URI")
    if len(req.logo_data or "") > 200_000:
        raise HTTPException(400, "The logo is too large (at most about 150 KB)")
    for field in ("name", "unknown_host_title"):
        if len(getattr(req, field) or "") > 80:
            raise HTTPException(400, f"{field} is too long (at most 80 characters)")
    if len(req.unknown_host_message or "") > 500:
        raise HTTPException(400, "unknown_host_message is too long (at most 500 characters)")
    brand = await _syscfg.set_brand(db, req.model_dump())
    html = nginx_pages.generate_cloudbase_unknown_host_html(_syscfg.get_panel_https_domain(), brand)
    ok, msg = await asyncio.to_thread(nginx_files.write_cloudbase_unknown_host_page, html)
    if not ok:
        log.warning("Writing the unknown-host page failed: %s", msg)
        raise HTTPException(500, "The brand is saved, but the page could not be written; try again")
    return brand


@router.get("/api/system/capacity")
async def get_capacity(db: AsyncSession = Depends(get_db), _: dict = Depends(auth.require_permission("nodes.view", "system.platform"))):
    """Reservations (nodes/capacity.py): the settings, what every node gives out and has promised, and the totals."""
    return await capacity.report(db)


@router.put("/api/system/capacity")
async def set_capacity(body: dict = Body(...), db: AsyncSession = Depends(get_db),
                       _: dict = Depends(auth.require_permission("system.manage", "system.platform"))):
    """Change the reservation settings. What already runs stays; only what is placed from now on follows them."""
    clean = capacity.clean_settings(body or {})
    if clean:
        _cfg.save_config_sections({"capacity": clean})
    return await capacity.report(db)


class DbDomainRequest(BaseModel):
    db_domain: Optional[str] = None
    # A wildcard certificate of your own (PEM text) for *.<db domain>, so clients trust the connection without the
    # Cloudbase CA. Without it the databases use the certificate of Cloudbase's own CA.
    ssl_cert_pem: Optional[str] = None
    ssl_key_pem: Optional[str] = None


@router.post("/api/system/db-domain")
async def set_db_domain(req: DbDomainRequest, db: AsyncSession = Depends(get_db), _: dict = Depends(auth.require_permission("system.manage", "system.platform"))):
    """Set (or clear) the domain public databases are reached under: <database name>.<db domain>. The wildcard record
    has to point at the primary and stay DNS only. Without it a database is reached on the primary's IP."""
    domain = str(req.db_domain or "").strip().strip(".").lower()
    if domain and not re.match(r'^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$', domain):
        raise HTTPException(400, "Invalid domain")
    cert_path = key_path = ""
    info = None
    if (req.ssl_cert_pem or "").strip() or (req.ssl_key_pem or "").strip():
        if not domain:
            raise HTTPException(400, "Enter the domain the certificate is for")
        try:
            cert_path, key_path, info = await asyncio.to_thread(
                own_certificate.save, req.ssl_cert_pem or "", req.ssl_key_pem or "", domain, "db")
        except own_certificate.CertificateProblem as e:
            raise HTTPException(400, str(e))
    await _syscfg.set_db_domain(db, domain, cert_path, key_path)
    await asyncio.to_thread(own_certificate.forget_except, cert_path or None, "db")
    await sync_public_access()
    return {"ok": True, "db_domain": domain, "own_certificate": info}


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
            "db_proxy_min": _cfg.get_port("db_proxy_min"),
            "db_proxy_max": _cfg.get_port("db_proxy_max"),
            "db_public_min": _cfg.get_port("db_public_min"),
            "db_public_max": _cfg.get_port("db_public_max"),
        },
        "wireguard": {"endpoint": _cfg.get_wireguard().get("endpoint") or ""},
        "logs": _cfg.get_logs(),
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
    sections = clean_system_settings(body)
    if sections:
        _cfg.save_config_sections(sections)
        if "logs" in sections:
            asyncio.create_task(log_settings.hand_out())      # this machine and every node, at once
    return {"ok": True}


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
