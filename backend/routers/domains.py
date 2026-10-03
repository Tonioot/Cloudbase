"""Domain setup helpers for the "Connect a domain" wizard.

- GET  /api/domains/server        public IP to point DNS at, and whether HTTPS can be automated
- POST /api/domains/check         does this name resolve here, and does port 80 reach us?
- POST /api/domains/certificate   request a free Let's Encrypt certificate
- GET  /api/domains/certificate/{name}   expiry date and names of a certificate
"""
import asyncio
import ipaddress
import logging
import os
import re
import secrets
import socket
import time
import urllib.error
import urllib.request
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

import auth as _auth
import certificates as certs
import nginx_manager as nm
from database import get_db

log = logging.getLogger("cloudbase.domains")
router = APIRouter(prefix="/api/domains", tags=["domains"])

_DOMAIN_RE = certs.DOMAIN_RE

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


# Kept for callers that import it from here
forget_pending = certs.forget_pending


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


def _local_ipv4() -> Optional[str]:
    """The address this server uses on its own network (no traffic is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("1.1.1.1", 80))
            return sock.getsockname()[0]
    except OSError:
        return None


def _local_ipv6_addresses() -> set[str]:
    try:
        import psutil
        return {
            a.address.split("%")[0]
            for addrs in psutil.net_if_addrs().values()
            for a in addrs
            if a.family == socket.AF_INET6
        }
    except Exception:
        return set()


def _resolve_cached(domain: str) -> list[str]:
    """The server's own resolver — can be minutes to hours behind (it caches
    answers, including "doesn't exist"). Only used as a fallback."""
    try:
        infos = socket.getaddrinfo(domain, 80, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return []
    return sorted({i[4][0] for i in infos})


def _resolve_authoritative(domain: str, depth: int = 0) -> Optional[list[str]]:
    """Ask the domain's own nameservers directly, so a record shows up the
    moment it's saved at the DNS provider. Returns None when that isn't
    possible (no dnspython, nameservers unreachable)."""
    try:
        import dns.message
        import dns.name
        import dns.query
        import dns.rdatatype
        import dns.resolver
    except ImportError:
        return None
    if depth > 4:
        return None
    try:
        name = dns.name.from_text(domain)
        zone = dns.resolver.zone_for_name(name, lifetime=5)
        nameservers = [r.target.to_text() for r in dns.resolver.resolve(zone, "NS", lifetime=5)]
    except Exception:
        return None

    for ns in nameservers[:4]:
        try:
            ns_ips = [r.address for r in dns.resolver.resolve(ns, "A", lifetime=5)]
        except Exception:
            continue
        for ns_ip in ns_ips[:2]:
            try:
                ips: set[str] = set()
                cname = None
                for rdtype in ("A", "AAAA"):
                    resp = dns.query.udp(dns.message.make_query(name, rdtype), ns_ip, timeout=4)
                    for rrset in resp.answer:
                        for rr in rrset:
                            if rr.rdtype in (dns.rdatatype.A, dns.rdatatype.AAAA):
                                ips.add(rr.address)
                            elif rr.rdtype == dns.rdatatype.CNAME:
                                cname = rr.target.to_text().rstrip(".")
                if not ips and cname:
                    return _resolve_authoritative(cname, depth + 1)
                return sorted(ips)
            except Exception:
                continue
    return None


def _resolve(domain: str) -> list[str]:
    fresh = _resolve_authoritative(domain)
    return fresh if fresh is not None else _resolve_cached(domain)


def _fetch_via(ip: str, domain: str, path: str, timeout: float = 6.0) -> tuple[int, str]:
    """GET http://<domain><path>, connecting straight to `ip` — so the test
    uses the fresh DNS answer instead of whatever this server has cached."""
    import http.client
    conn = http.client.HTTPConnection(ip, 80, timeout=timeout)
    try:
        conn.request("GET", path, headers={"Host": domain, "User-Agent": "Cloudbase-domain-check"})
        resp = conn.getresponse()
        return resp.status, resp.read(256).decode("ascii", errors="ignore")
    except Exception as e:
        return 0, str(e)
    finally:
        conn.close()


@router.get("/server")
async def server_info(_user: dict = Depends(_auth.require_permission("apps.view"))):
    ok, reason = certs.capability()
    public_ip = await _server_ip()
    local_ip = _local_ipv4()
    behind_nat = False
    try:
        behind_nat = bool(local_ip and public_ip and local_ip != public_ip and ipaddress.ip_address(local_ip).is_private)
    except ValueError:
        pass
    return {
        "ip": public_ip,
        "local_ip": local_ip,
        # Behind a router: ports 80/443 have to be forwarded to local_ip
        "behind_nat": behind_nat,
        "https_available": ok,
        "https_reason": reason,
        "email": certs.acme_email(),
    }


class CheckRequest(BaseModel):
    domain: str


@router.post("/check")
async def check_domain(req: CheckRequest, _user: dict = Depends(_auth.require_permission("apps.configure"))):
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
        "warnings": [],
    }
    # Let's Encrypt prefers IPv6: an AAAA record that doesn't lead here makes
    # the certificate request fail even when the IPv4 record is right.
    stray_v6 = [ip for ip in ips if ":" in ip and not _is_cloudflare(ip) and ip not in _local_ipv6_addresses()]
    if stray_v6:
        result["warnings"].append(
            f"{domain} also has an IPv6 (AAAA) record: {', '.join(stray_v6)}. Let’s Encrypt tries IPv6 first — "
            "remove that record unless it points to this server, or the certificate request will fail."
        )
    if not ips:
        result["detail"] = "No DNS record found yet. Most providers publish it within a few minutes."
        return result
    if result["cloudflare_proxy"]:
        result["detail"] = ("This domain goes through Cloudflare’s proxy (orange cloud) and the test didn’t get through. "
                            "Turn off “Always Use HTTPS” in Cloudflare, or set the record to DNS only (grey) while you set up HTTPS.")
    elif server_ip and server_ip not in ips:
        result["detail"] = f"{domain} points to {', '.join(ips)}, not to this server ({server_ip})."

    await certs.mark_pending([domain])

    token = "cloudbase-check-" + secrets.token_hex(8)
    value = secrets.token_hex(16)
    wrote, msg = nm.write_acme_probe(token, value)
    if not wrote:
        result["detail"] = result["detail"] or "Couldn’t run the reachability test on this server (run cloudbase nginx permissions)."
        return result
    try:
        path = f"/.well-known/acme-challenge/{token}"
        target = server_ip if server_ip in ips else ips[0]
        status, body = await asyncio.to_thread(_fetch_via, target, domain, path)
        if status == 200 and body.strip() == value:
            result["reachable"] = True
            result["dns_ok"] = True
            result["detail"] = ""
        else:
            # Through the public address it didn't work. Ask nginx on this
            # machine directly to tell "nginx isn't ready" apart from "this
            # server can't reach its own public IP" (home routers without NAT
            # loopback answer with their own page instead).
            local_status, local_body = await asyncio.to_thread(_fetch_via, "127.0.0.1", domain, path)
            local_ok = local_status == 200 and local_body.strip() == value
            result["nginx_ready"] = local_ok
            if result["cloudflare_proxy"] or (server_ip and server_ip not in ips):
                pass  # keep the DNS explanation set above
            elif local_ok:
                result["detail"] = (
                    "DNS points here and nginx is ready. This server can’t reach itself through its public address "
                    "(normal behind a home router), so the final check happens when the certificate is requested — "
                    "make sure ports 80 and 443 are forwarded to this server."
                )
            elif local_status and local_status < 500:
                result["detail"] = (
                    f"A site answered for {domain} instead of the verification folder (HTTP {local_status}). "
                    "Click Save Changes in the app’s settings to rewrite its nginx config, "
                    "and remove any old nginx config for this domain from /etc/nginx/sites-enabled."
                )
            else:
                result["detail"] = (
                    f"{domain} points here, but the test request didn’t come back (HTTP {status or 'no response'}). "
                    "Check that nginx is running and port 80 is open."
                )
    finally:
        nm.remove_acme_probe(token)
    return result


class CertificateRequest(BaseModel):
    domains: list[str]
    email: Optional[str] = None
    # Existing certificate to update instead of creating a new one, so its
    # /etc/letsencrypt/live/<cert_name>/ paths stay the same
    cert_name: Optional[str] = None


@router.post("/certificate")
async def request_certificate(req: CertificateRequest, _user: dict = Depends(_auth.require_permission("apps.configure"))):
    domains = list(dict.fromkeys(_clean_domain(d) for d in req.domains))
    email = (req.email or "").strip()
    if email:
        try:
            import config as _cfg
            _cfg.save_config_sections({"acme": {"email": email}})
        except Exception as e:
            log.warning("Could not remember ACME email: %s", e)
    try:
        cert, key = await certs.issue(domains, cert_name=req.cert_name, email=email or None)
    except certs.CertificateError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "domains": domains, "ssl_cert_path": cert, "ssl_key_path": key}


@router.get("/certificate/{cert_name}")
async def certificate_info(cert_name: str, _user: dict = Depends(_auth.require_permission("apps.view"))):
    """Expiry and names of a Cloudbase-managed certificate."""
    cert_name = cert_name.strip().lower()
    if not _DOMAIN_RE.match(cert_name):
        raise HTTPException(400, "Invalid certificate name")
    data = await asyncio.to_thread(certs.info, cert_name)
    if not data:
        return {"found": False}
    return {"found": True, **data}


@router.delete("/certificate/{cert_name}")
async def delete_certificate(cert_name: str, _user: dict = Depends(_auth.require_permission("apps.configure"))):
    """Remove a certificate no app uses anymore, so certbot stops renewing it."""
    cert_name = cert_name.strip().lower()
    if not _DOMAIN_RE.match(cert_name):
        raise HTTPException(400, "Invalid certificate name")
    return {"ok": await asyncio.to_thread(certs.delete, cert_name)}
