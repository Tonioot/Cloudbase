import asyncio
import logging
import subprocess
import time
from typing import Optional
from nginx import files as nginx_files
from core import config as _cfg
from nginx.certbot import DOMAIN_RE, EMAIL_RE, _forget, _helper, capability, exists, paths

log = logging.getLogger("cloudbase.certificates")


# ── Temporary verification server for names not routed to an app yet ──────
#
# Gives a domain a port-80 server that only answers Let's Encrypt (and the
# wizard's check) — also through Cloudflare's proxy. Its config sorts last,
# so an app's own server block for the same name always takes precedence.

_PENDING_TTL = 3600


_pending: dict[str, float] = {}


_acme_written: tuple = ()


_acme_lock = asyncio.Lock()


async def _sync_acme_server() -> tuple[bool, str]:
    global _acme_written
    async with _acme_lock:
        now = time.time()
        for d, expires in list(_pending.items()):
            if expires < now:
                del _pending[d]
        names = tuple(sorted(_pending))
        if names == _acme_written:
            return True, ""
        if names:
            ok, msg = await asyncio.to_thread(nginx_files.write_acme_server, list(names))
        else:
            await asyncio.to_thread(nginx_files.remove_acme_server)
            ok, msg = True, ""
        if ok:
            _acme_written = names
        return ok, msg


async def mark_pending(domains: list[str]) -> tuple[bool, str]:
    for d in domains:
        _pending[d] = time.time() + _PENDING_TTL
    return await _sync_acme_server()


async def forget_pending(domains: list[str]) -> None:
    """Called once domains are configured on an app (or set up is done)."""
    changed = False
    for d in domains:
        if _pending.pop((d or "").strip().lower(), None) is not None:
            changed = True
    if changed:
        await _sync_acme_server()


# ── Issuing ────────────────────────────────────────────────────────────────

_issue_lock = asyncio.Lock()


class CertificateError(Exception):
    pass


def acme_email() -> str:
    return (_cfg._config.get("acme") or {}).get("email") or ""


async def issue(domains: list[str], cert_name: Optional[str] = None, email: Optional[str] = None) -> tuple[str, str]:
    """Request a certificate (or update the names on `cert_name`). Returns the
    (fullchain, privkey) paths. Raises CertificateError with a readable reason."""
    domains = [d.strip().lower() for d in domains if d and d.strip()]
    domains = list(dict.fromkeys(domains))
    if not domains:
        raise CertificateError("Add at least one domain")
    if len(domains) > 10:
        raise CertificateError("At most 10 domains per certificate")
    for d in domains:
        if not DOMAIN_RE.match(d):
            raise CertificateError(f"“{d}” isn’t a valid domain name")
    cert_name = (cert_name or "").strip().lower() or domains[0]
    if not DOMAIN_RE.match(cert_name):
        raise CertificateError("Invalid certificate name")
    email = (email or "").strip() or acme_email()
    if email and not EMAIL_RE.match(email):
        raise CertificateError("That email address doesn’t look right")
    ok, reason = capability()
    if not ok:
        raise CertificateError(reason)

    async with _issue_lock:
        acme_ok, acme_msg = await mark_pending(domains)
        if not acme_ok:
            raise CertificateError(f"Couldn’t prepare nginx for verification: {acme_msg}")
        try:
            proc = await asyncio.to_thread(
                _helper, "issue", cert_name, email or "-", *domains, timeout=240,
            )
        except subprocess.TimeoutExpired:
            raise CertificateError("Let’s Encrypt didn’t answer in time. Try again in a minute.")
        finally:
            await forget_pending(domains)

    _forget(cert_name)
    log.info("[certificate] issue %s for %s rc=%d", cert_name, domains, proc.returncode)
    if proc.returncode != 0:
        raise CertificateError(explain_error((proc.stdout or "") + (proc.stderr or "")))
    return paths(cert_name)


def explain_error(output: str) -> str:
    low = output.lower()
    if "too many certificates" in low or "ratelimit" in low or "rate limit" in low:
        return "Let’s Encrypt’s rate limit was hit for this domain. Wait an hour and try again."
    if "dns problem" in low or "nxdomain" in low:
        return "Let’s Encrypt couldn’t find a DNS record for the domain. Check the record and wait a few minutes."
    if "connection refused" in low or "timeout during connect" in low or "fetching http" in low:
        return "Let’s Encrypt couldn’t reach the server on port 80. Make sure port 80 is open to the internet."
    if "unauthorized" in low or "invalid response" in low:
        return "Let’s Encrypt reached a different server for this domain. Check that the DNS record points to this server."
    if "sudo" in low and ("password" in low or "not allowed" in low):
        return "Cloudbase isn’t allowed to request certificates yet. Run cloudbase nginx permissions on the server."
    last = [l for l in output.strip().splitlines() if l.strip()][-3:]
    return "Requesting the certificate failed: " + (" ".join(last) or "unknown error")


# ── Automatic certificates for app subdomains (<app>.<base domain>) ────────
#
# Let's Encrypt can only issue a wildcard (*.apps.example.com) through DNS
# verification, which needs access to the DNS provider. Instead every app
# gets its own certificate for its subdomain, requested in the background.

_auto_next_try: dict[str, float] = {}


_auto_running: set[str] = set()


_AUTO_RETRY_SECONDS = 3600


def wants_auto_certificate(name: str) -> bool:
    """Should we try to get a certificate for this subdomain now?"""
    if not name or name in _auto_running or _auto_next_try.get(name, 0) > time.time():
        return False
    if not capability()[0]:
        return False
    return not exists(name)


async def ensure_auto_certificate(name: str, on_ready) -> None:
    """Request a certificate for `name` and call `on_ready()` (async) when it's there."""
    if name in _auto_running:
        return
    _auto_running.add(name)
    try:
        await issue([name], cert_name=name)
        _auto_next_try.pop(name, None)
        log.info("[certificate] automatic certificate ready for %s", name)
        await on_ready()
    except CertificateError as e:
        _auto_next_try[name] = time.time() + _AUTO_RETRY_SECONDS
        log.warning("[certificate] automatic certificate for %s failed (retry in 1h): %s", name, e)
    except Exception as e:
        _auto_next_try[name] = time.time() + _AUTO_RETRY_SECONDS
        log.exception("[certificate] automatic certificate for %s failed: %s", name, e)
    finally:
        _auto_running.discard(name)
