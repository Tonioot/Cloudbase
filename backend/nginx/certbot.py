import logging
import os
import re
import shutil
import subprocess
import time
from datetime import timezone, datetime
from typing import Optional
from nginx import paths as nginx_paths

log = logging.getLogger("cloudbase.certificates")


HELPER = "/usr/local/lib/cloudbase/issue-cert"


LIVE_DIR = "/etc/letsencrypt/live"


DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}$")


_LIVE_PATH_RE = re.compile(r"^/etc/letsencrypt/live/([a-z0-9.-]+)/(fullchain|privkey)\.pem$")


def paths(cert_name: str) -> tuple[str, str]:
    return f"{LIVE_DIR}/{cert_name}/fullchain.pem", f"{LIVE_DIR}/{cert_name}/privkey.pem"


def name_from_path(path: Optional[str]) -> Optional[str]:
    """Name of a Cloudbase-managed certificate, from its file path (else None)."""
    m = _LIVE_PATH_RE.match(path or "")
    return m.group(1) if m and DOMAIN_RE.match(m.group(1)) else None


def capability() -> tuple[bool, str]:
    """Can this server request certificates by itself?"""
    if not shutil.which("certbot"):
        return False, "certbot isn’t installed on the server. Run cloudbase update, then cloudbase nginx permissions."
    if not os.path.exists(HELPER):
        return False, "Certificate permissions aren’t set up yet. Run cloudbase nginx permissions on the server."
    if not os.access(os.path.join(nginx_paths.ACME_WEBROOT, ".well-known", "acme-challenge"), os.W_OK):
        return False, "The verification folder isn’t writable. Run cloudbase nginx permissions on the server."
    return True, ""


def _helper(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["sudo", "-n", HELPER, *args], capture_output=True, text=True, timeout=timeout)


# ── Existence & details (cached; /etc/letsencrypt is root-only) ────────────

_exists_cache: dict[str, tuple[bool, float]] = {}


_info_cache: dict[str, tuple[Optional[dict], float]] = {}


def exists(cert_name: str) -> bool:
    """True when the certificate is present. If the helper can't answer
    (missing or outdated) assume it exists and let `nginx -t` be the judge."""
    hit = _exists_cache.get(cert_name)
    if hit and hit[1] > time.time():
        return hit[0]
    try:
        r = _helper("exists", cert_name, timeout=10)
        result = r.returncode == 0 if r.returncode in (0, 1) else True
    except Exception:
        result = True
    _exists_cache[cert_name] = (result, time.time() + (600 if result else 60))
    return result


def info(cert_name: str) -> Optional[dict]:
    """{"expires_at": iso, "days_left": int, "names": [...]} or None."""
    hit = _info_cache.get(cert_name)
    if hit and hit[1] > time.time():
        return hit[0]
    data = None
    try:
        r = _helper("info", cert_name, timeout=10)
        if r.returncode == 0:
            fields = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
            expires = datetime.strptime(fields.get("expires", "").strip(), "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
            data = {
                "expires_at": expires.isoformat(),
                "days_left": (expires - datetime.now(timezone.utc)).days,
                "names": [n for n in fields.get("names", "").split(",") if n],
            }
    except Exception as e:
        log.debug("certificate info for %s failed: %s", cert_name, e)
    _info_cache[cert_name] = (data, time.time() + 600)
    return data


def _forget(cert_name: str) -> None:
    _exists_cache.pop(cert_name, None)
    _info_cache.pop(cert_name, None)


def delete(cert_name: str) -> bool:
    if not DOMAIN_RE.match(cert_name or ""):
        return False
    try:
        r = _helper("delete", cert_name, timeout=60)
    except Exception as e:
        log.warning("Could not remove certificate %s: %s", cert_name, e)
        return False
    _forget(cert_name)
    log.info("[certificate] delete %s rc=%d", cert_name, r.returncode)
    return r.returncode == 0
