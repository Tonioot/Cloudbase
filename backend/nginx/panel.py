import asyncio
import logging
import os
from sqlalchemy import select
from core.database import AsyncSessionLocal
from core.models import Application
from nginx import manager as nm
from core import system_config as _syscfg
from nodes.registry import ensure_local_node as _eln
from apps.nginx_sync import _write_app_nginx_config as _wanc
from core.runtime import PORT

log = logging.getLogger("cloudbase.main")


# ── Lifespan ──────────────────────────────────────────────────────────────────
def read_panel_nginx() -> dict:
    """Domain and certificate of the panel, read back from its nginx config."""
    import re as _re
    path = os.path.join(nm.NGINX_SITES_DIR, "cloudbase")
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return {}
    def _val(pattern):
        m = _re.search(pattern, content)
        return m.group(1).strip("\"'") if m else None
    return {
        "domain": _val(r"server_name\s+([^\s;]+)"),
        "ssl_cert_path": _val(r"ssl_certificate\s+([^\s;]+)"),
        "ssl_key_path": _val(r"ssl_certificate_key\s+([^\s;]+)"),
    }


def _load_panel_https_domain() -> None:
    """With an HTTPS panel domain the panel is only served through nginx: direct
    requests to this port from other machines go to https://<domain>, so passwords
    and session cookies never cross the network unencrypted."""
    panel = read_panel_nginx()
    https = panel.get("domain") and panel.get("ssl_cert_path") and panel.get("ssl_key_path")
    _syscfg.set_panel_https_domain(panel["domain"] if https else None)


def _refresh_panel_nginx() -> None:
    panel = read_panel_nginx()
    if not panel.get("domain"):
        return
    cert, key = panel.get("ssl_cert_path"), panel.get("ssl_key_path")
    config = nm.generate_config("cloudbase", panel["domain"], PORT, cert, key, strict_hostnames=True)
    ok, msg = nm.write_nginx_config("cloudbase", config)
    if not ok:
        log.warning("STARTUP panel nginx rewrite failed: %s", msg)
        return
    nm.write_default_catch_all(cert, key)


async def _refresh_all_app_nginx() -> None:
    """Rewrite every app's nginx config (after base domain changes). This also
    schedules automatic certificates for apps on the base domain."""
    try:
        async with AsyncSessionLocal() as _db:
            _apps = (await _db.execute(select(Application))).scalars().all()
            _local = await _eln(_db)
            _bdom = _syscfg.get_base_domain_cached()
            for _a in _apps:
                if not (_a.nginx_enabled and _a.domain) and not _bdom:
                    if not _a.domain:
                        # Was only reachable through the base domain, which is gone
                        await asyncio.to_thread(nm.remove_nginx_config, _a.name)
                    continue
                try:
                    await _wanc(_a, _db, _local)
                except Exception as _e:
                    log.warning("nginx refresh failed for app %s: %s", _a.name, _e)
    except Exception as _e:
        log.warning("nginx refresh batch failed: %s", _e)
