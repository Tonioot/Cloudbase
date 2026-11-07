"""The primary's own public IP address, so nodes can find it without a domain name.

The panel's domain often points at a proxy (Cloudflare) that only carries HTTP, while the node
network needs the real address of this server. It is looked up once an hour from a service that
answers with the caller's address, and only when no address was configured by hand.
"""
import ipaddress
import time
import urllib.request

SERVICES = ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com")
CACHE_SECONDS = 3600

_cache = {"ip": None, "at": 0.0}


def _fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "cloudbase"})
    with urllib.request.urlopen(request, timeout=5) as resp:
        return resp.read(100).decode("ascii", "ignore").strip()


def detect() -> str | None:
    """The public IPv4 address of this server, or None when it cannot be found."""
    now = time.time()
    if now - _cache["at"] < CACHE_SECONDS:
        return _cache["ip"]
    found = None
    for url in SERVICES:
        try:
            ip = ipaddress.ip_address(_fetch(url))
        except (OSError, ValueError):
            continue
        if ip.version == 4 and ip.is_global:
            found = str(ip)
            break
    _cache["ip"], _cache["at"] = found, now
    return found
