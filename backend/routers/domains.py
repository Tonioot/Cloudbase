"""Domain setup helpers for the "Connect a domain" wizard.

- GET  /api/domains/server        public IP to point DNS at, and whether HTTPS can be automated
- POST /api/domains/check         does this name resolve here, and does port 80 reach us?
- POST /api/domains/certificate   request a free Let's Encrypt certificate
"""
import asyncio
import ipaddress
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

import auth as _auth
import config as _cfg
import nginx_manager as nm
from database import get_db

log = logging.getLogger("cloudbase.domains")
router = APIRouter(prefix="/api/domains", tags=["domains"])

ISSUE_CERT_BIN = "/usr/local/lib/cloudbase/issue-cert"
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}$")

# Cloudflare's published proxy ranges (cloudflare.com/ips-v4, ips-v6)
_CLOUDFLARE_NETS = [ipaddress.ip_network(n) for n in (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22", "141.101.64.0/18",
    "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22", "198.41.128.0/17",
    "162.158.0.0/15", "104.16.0.0/13", "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32", "2405:8100::/32",
    "2a06:98c0::/29", "2c0f:f248::/32",
)]


def _is_cloudflare(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in _CLOUDFLARE_NETS if net.version == addr.version)


_server_ip_cache: dict = {"ip": None, "at": 0.0}
_issue_lock = asyncio.Lock()


def _clean_domain(value: str) -> str:
    d = (value or "").strip().lower()
    d = re.sub(r"^[a-z]+://", "", d).split("/")[0].split(":")[0].strip(".")
    if not _DOMAIN_RE.match(d):
        raise HTTPException(400, f"“{value}” isn’t a valid domain name")
    return d


async def _server_ip() -> Optional[str]:
    if _server_ip_cache["ip"] and time.time() - _server_ip_cache["at"] < 600:
        return _server_ip_cache["ip"]
    from routers.nodes import _detect_public_ip
    ip = await asyncio.to_thread(_detect_public_ip)
    if ip:
        _server_ip_cache.update(ip=ip, at=time.time())
    return ip


def _https_capability() -> tuple[bool, str]:
    """Can this server request certificates by itself?"""
    if not shutil.which("certbot"):
        return False, "certbot isn’t installed on the server. Run cloudbase update, then cloudbase nginx permissions."
    if not os.path.exists(ISSUE_CERT_BIN):
        return False, "Certificate permissions aren’t set up yet. Run cloudbase nginx permissions on the server."
    if not os.access(os.path.join(nm.ACME_WEBROOT, ".well-known", "acme-challenge"), os.W_OK):
        return False, "The verification folder isn’t writable. Run cloudbase nginx permissions on the server."
    return True, ""


def _resolve(domain: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(domain, 80, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return []
    return sorted({i[4][0] for i in infos})


def _fetch(url: str, timeout: float = 6.0) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "Cloudbase-domain-check"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(256).decode("ascii", errors="ignore")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        return 0, str(e)


@router.get("/server")
async def server_info(_user: dict = Depends(_auth.require_permission("apps.view"))):
    ok, reason = _https_capability()
    return {
        "ip": await _server_ip(),
        "https_available": ok,
        "https_reason": reason,
        "email": (_cfg._config.get("acme") or {}).get("email") or "",
    }


class CheckRequest(BaseModel):
    domain: str


@router.post("/check")
async def check_domain(req: CheckRequest, _user: dict = Depends(_auth.require_permission("apps.view"))):
    """DNS lookup plus a real HTTP round trip: a token file is placed in the
    challenge folder and fetched through the domain, exactly like Let's
    Encrypt will do. That proves both DNS and port 80 are right."""
    domain = _clean_domain(req.domain)
    server_ip = await _server_ip()
    ips = await asyncio.to_thread(_resolve, domain)

    result = {
        "domain": domain,
        "server_ip": server_ip,
        "ips": ips,
        "dns_ok": bool(ips) and (server_ip in ips if server_ip else True),
        "reachable": False,
        "cloudflare_proxy": bool(ips) and all(_is_cloudflare(ip) for ip in ips),
        "detail": "",
    }
    if not ips:
        result["detail"] = "No DNS record found yet. New records can take a few minutes to appear."
        return result
    if result["cloudflare_proxy"]:
        result["detail"] = ("This domain goes through Cloudflare’s proxy (orange cloud). Set it to DNS only (grey) "
                            "while you set up HTTPS — you can turn the proxy back on afterwards.")
    elif server_ip and server_ip not in ips:
        result["detail"] = f"{domain} points to {', '.join(ips)}, not to this server ({server_ip})."

    token = "cloudbase-check-" + secrets.token_hex(8)
    value = secrets.token_hex(16)
    wrote, msg = nm.write_acme_probe(token, value)
    if not wrote:
        result["detail"] = result["detail"] or "Couldn’t run the reachability test on this server (run cloudbase nginx permissions)."
        return result
    try:
        status, body = await asyncio.to_thread(_fetch, f"http://{domain}/.well-known/acme-challenge/{token}")
        if status == 200 and body.strip() == value:
            result["reachable"] = True
            result["dns_ok"] = True
            result["detail"] = ""
        elif not result["detail"]:
            result["detail"] = (
                f"{domain} points here, but the test request didn’t come back (HTTP {status or 'no response'}). "
                "Check that port 80 is open. Behind a home router or NAT this test can fail even when it works from outside."
            )
    finally:
        nm.remove_acme_probe(token)
    return result


class CertificateRequest(BaseModel):
    domains: list[str]
    email: Optional[str] = None


@router.post("/certificate")
async def request_certificate(req: CertificateRequest, _user: dict = Depends(_auth.require_permission("apps.configure"))):
    domains = []
    for d in req.domains:
        c = _clean_domain(d)
        if c not in domains:
            domains.append(c)
    if not domains:
        raise HTTPException(400, "Add at least one domain")
    if len(domains) > 10:
        raise HTTPException(400, "At most 10 domains per certificate")

    email = (req.email or "").strip()
    if email and not _EMAIL_RE.match(email):
        raise HTTPException(400, "That email address doesn’t look right")

    ok, reason = _https_capability()
    if not ok:
        raise HTTPException(400, reason)

    if email:
        try:
            _cfg.save_config_sections({"acme": {"email": email}})
        except Exception as e:
            log.warning("Could not remember ACME email: %s", e)

    async with _issue_lock:
        # Serve challenges for names that aren't routed to an app yet
        acme_ok, acme_msg = await asyncio.to_thread(nm.write_acme_server, domains)
        if not acme_ok:
            raise HTTPException(500, f"Couldn’t prepare nginx for verification: {acme_msg}")
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                ["sudo", "-n", ISSUE_CERT_BIN, email or "-", *domains],
                capture_output=True, text=True, timeout=240,
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "Let’s Encrypt didn’t answer in time. Try again in a minute.")
        finally:
            await asyncio.to_thread(nm.remove_acme_server)

    output = (proc.stdout or "") + (proc.stderr or "")
    log.info("[certificate] domains=%s rc=%d", domains, proc.returncode)
    if proc.returncode != 0:
        raise HTTPException(400, _explain_certbot_error(output))

    live = f"/etc/letsencrypt/live/{domains[0]}"
    return {
        "ok": True,
        "domains": domains,
        "ssl_cert_path": f"{live}/fullchain.pem",
        "ssl_key_path": f"{live}/privkey.pem",
    }


def _explain_certbot_error(output: str) -> str:
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
