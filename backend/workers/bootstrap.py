"""What the installer was asked to set up (install.sh --tenant --panel-domain … --api-token …), done once by the panel.

install.sh turns tenant mode on itself (before Cloudbase first starts), writes ~/.cloudbase/bootstrap.json and starts Cloudbase; this does the rest with the code the panel uses for it
anyway, so a server can be set up from one command, with nothing to click (a platform built on Cloudbase uses this):

    api_token       an API token with this value and these permissions (only its hash is kept; the value leaves the file)
    base_domain     app subdomains: <app>.<base domain>, HTTPS per app, automatic
    db_domain       database hostnames: <database>.<db domain>
    panel_domain    the panel at this domain, with its own certificate. Let's Encrypt needs the DNS record to point here:
                    tried again every 30 seconds, for a day

Every step is written down as done, so a restart goes on where it was; when everything is done the file is removed.
"""
import asyncio
import json
import logging
import os
from datetime import datetime

from sqlalchemy import select

from core import auth, system_config as _syscfg
from core.database import AsyncSessionLocal
from core.models import ApiToken, Permission
from dbs.public import sync_public_access
from nginx import certificates as nginx_certificates
from nginx.panel import _refresh_all_app_nginx, apply_panel_domain

log = logging.getLogger("cloudbase.bootstrap")

PATH = os.path.expanduser("~/.cloudbase/bootstrap.json")
RETRY_SECONDS = 30
GIVE_UP_SECONDS = 24 * 3600

# What a platform on Cloudbase needs: its apps, databases and projects, adding nodes, its own settings (system.platform).
# Never users, roles, tokens, the server's own settings or secrets in clear text.
PLATFORM_PERMISSIONS = [
    "apps.view", "apps.deploy", "apps.start", "apps.stop", "apps.restart", "apps.pull", "apps.scale", "apps.configure",
    "apps.delete", "projects.manage", "databases.view", "databases.create", "databases.manage", "databases.credentials",
    "databases.backup", "databases.delete", "nodes.view", "nodes.add", "system.platform",
]


def _read() -> dict | None:
    try:
        with open(PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write(data: dict) -> None:
    part = PATH + ".part"
    with open(os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(part, PATH)


def _done(data: dict, step: str, **remove) -> None:
    data.setdefault("done", []).append(step)
    for key in remove:
        data.pop(key, None)
    _write(data)


async def _token(data: dict) -> None:
    value = str(data.get("api_token") or "")
    if not value.startswith(auth.TOKEN_PREFIX) or len(value) < 40:
        raise ValueError("api_token must start with cbt_ and be at least 40 characters")
    async with AsyncSessionLocal() as db:
        known = set((await db.execute(select(Permission.name))).scalars())
        wanted = [p for p in (data.get("api_token_permissions") or PLATFORM_PERMISSIONS) if p in known]
        token_hash = auth.hash_api_token(value)
        if (await db.execute(select(ApiToken).where(ApiToken.token_hash == token_hash))).scalar_one_or_none() is None:
            db.add(ApiToken(name=str(data.get("api_token_name") or "platform")[:60], permissions=json.dumps(sorted(wanted)),
                            token_hash=token_hash, prefix=value[:len(auth.TOKEN_PREFIX) + 4], created_at=datetime.utcnow()))
            await db.commit()


async def _domains(data: dict) -> None:
    async with AsyncSessionLocal() as db:
        if data.get("base_domain"):
            await _syscfg.set_base_settings(db, base_domain=str(data["base_domain"]).strip().lower(), base_ssl_cert="", base_ssl_key="")
            asyncio.create_task(_refresh_all_app_nginx())
        if data.get("db_domain"):
            await _syscfg.set_db_domain(db, str(data["db_domain"]).strip().lower())
            await sync_public_access()


async def _panel(domain: str) -> None:
    cert, key = await nginx_certificates.issue([domain])
    await apply_panel_domain(domain, cert, key)


async def apply_pending() -> None:
    """Do what is left of the bootstrap file; the panel's certificate is tried again until it works (or a day passed)."""
    data = _read()
    if data is None:
        return
    done = set(data.get("done") or [])
    try:
        if data.get("api_token") and "api_token" not in done:
            await _token(data)
            _done(data, "api_token", api_token=True)              # the value is not kept in the file any longer
            log.info("[bootstrap] API token made")
        if (data.get("base_domain") or data.get("db_domain")) and "domains" not in done:
            await _domains(data)
            _done(data, "domains")
            log.info("[bootstrap] domains set: apps %s, databases %s", data.get("base_domain"), data.get("db_domain"))
    except Exception as e:
        log.error("[bootstrap] %s", e)
        return
    domain = str(data.get("panel_domain") or "").strip().lower()
    if domain and "panel" not in done:
        started = asyncio.get_running_loop().time()
        while True:
            try:
                await _panel(domain)
                _done(data, "panel")
                log.info("[bootstrap] the panel is at https://%s", domain)
                break
            except Exception as e:
                if asyncio.get_running_loop().time() - started > GIVE_UP_SECONDS:
                    log.error("[bootstrap] gave up on the panel's certificate for %s: %s", domain, e)
                    return
                log.warning("[bootstrap] the panel's certificate for %s is not there yet (%s); trying again", domain, e)
                await asyncio.sleep(RETRY_SECONDS)
    try:
        os.remove(PATH)
    except OSError:
        pass
