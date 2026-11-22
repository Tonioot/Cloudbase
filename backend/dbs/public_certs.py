"""Certificates for the public database ports.

A database that is reached by a name (<name>.<database domain>, or a hostname of its own) always gets a real
certificate, never one from Cloudbase's own CA:
  own          the wildcard certificate uploaded in Domain & SSL, for databases at <name>.<database domain>
  letsencrypt  one free certificate per hostname, requested like the app subdomains (HTTP-01 over port 80) and renewed
               by certbot. /etc/letsencrypt is root-only, so a copy is read through the sudo helper into
               ~/.cloudbase/certs/dbauto-<hostname>/ and refreshed every few hours.
  waiting      no certificate yet: the public port stays closed until there is one
Only a database on the bare IP address, which no public certificate can cover, keeps the Cloudbase CA ("cloudbase").
MySQL/MariaDB do TLS themselves end to end, and a database with TLS off needs none ("none").
"""
import asyncio
import hashlib
import logging
import os
import time
from typing import Awaitable, Callable, Optional

from core import system_config as _syscfg
from core.models import Database
from nginx import certbot as _certbot
from nginx import certificates as _certificates
from nginx.own_certificate import CERTS_DIR

log = logging.getLogger("cloudbase.db-certs")

# database id → "own" | "letsencrypt" | "waiting" | "cloudbase" | "none", refreshed by every sync of the public ports
STATE: dict[int, str] = {}

_REFRESH_SECONDS = 6 * 3600
_exported: dict[str, tuple[tuple[str, str, str], float]] = {}   # hostname → ((cert path, key path, fingerprint), valid until)


def is_trusted(database_id: int) -> bool:
    """Does the database use a certificate that clients already trust (no Cloudbase CA file needed)?"""
    return STATE.get(database_id) in ("own", "letsencrypt")


def needs_certificate(database: Database) -> bool:
    return bool(database.public_tls) and database.engine not in ("mysql", "mariadb")


def _read_pem(kind: str, host: str) -> Optional[str]:
    try:
        r = _certbot._helper(kind, host, timeout=20)
    except Exception as e:
        log.warning("[db-certs] could not read the certificate of %s: %s", host, e)
        return None
    if r.returncode != 0 or "-----BEGIN" not in (r.stdout or ""):
        log.warning("[db-certs] the helper could not give the certificate of %s (rc=%s). "
                    "Run cloudbase nginx permissions to update it.", host, r.returncode)
        return None
    return r.stdout.strip() + "\n"


def _write(path: str, text: str, mode: int) -> None:
    try:
        with open(path, encoding="ascii") as f:
            if f.read() == text:
                return
    except OSError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(text)
    os.chmod(path, mode)


def _export(host: str) -> Optional[tuple[str, str, str]]:
    """The Let's Encrypt certificate of `host` as files Cloudbase can read, or None while there is none."""
    hit = _exported.get(host)
    if hit and hit[1] > time.time():
        return hit[0]
    if not _certbot.exists(host):
        return None
    cert, key = _read_pem("export-cert", host), _read_pem("export-key", host)
    if not cert or not key:
        return None
    folder = os.path.join(CERTS_DIR, f"dbauto-{host}")
    os.makedirs(folder, mode=0o700, exist_ok=True)
    cert_path, key_path = os.path.join(folder, "fullchain.pem"), os.path.join(folder, "privkey.pem")
    _write(cert_path, cert, 0o644)
    _write(key_path, key, 0o600)
    result = (cert_path, key_path, hashlib.sha256(cert.encode()).hexdigest()[:12])
    _exported[host] = (result, time.time() + _REFRESH_SECONDS)
    return result


async def resolve(database: Database, host: str, on_ready: Callable[[], Awaitable[None]]) -> tuple[str, Optional[tuple]]:
    """(state, certificate) for a database reached at `host`. The certificate is (cert path, key path, fingerprint),
    or None for the Cloudbase CA / no TLS handling of ours / no certificate yet."""
    if not needs_certificate(database):
        return "none", None
    if not _certbot.DOMAIN_RE.match(host or ""):
        return "cloudbase", None                      # the bare IP address
    domain = _syscfg.get_db_domain_cached()
    own_cert, own_key = _syscfg.get_db_ssl_cached()
    if domain and own_cert and own_key and not database.public_hostname:
        return "own", (own_cert, own_key, "own")
    exported = await asyncio.to_thread(_export, host)
    if exported:
        return "letsencrypt", exported
    if _certificates.wants_auto_certificate(host):
        asyncio.create_task(_certificates.ensure_auto_certificate(host, on_ready))
    return "waiting", None
