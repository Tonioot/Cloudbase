"""What the app containers on this machine send to the public internet, per app.

The root helper (scripts/cloudbase-quota) keeps one counter per container address in the firewall, for packets that
leave through a Cloudbase bridge towards a public address; a private destination (nginx, a database, another app or
node, the local network) is not counted. Every collect() reads and zeroes those counters and hands the helper the
addresses of the containers running now. Instances and cron jobs both count for their app.

Blocking: call from a thread. Used by the primary (workers/traffic.py) and by the agent (agent/report.py).
"""
import logging
import re

from containers import quota
from containers.client import _get_client

log = logging.getLogger("cloudbase.traffic")

_NAME = re.compile(r"^cloudbase-(?:app-(\d+)-instance-\d+|cron-(\d+)-\d+)$")

_counted: dict[str, int] = {}     # address -> app id, of what the helper counts until the next collect()


def container_addresses() -> dict[str, int]:
    """{address: app id} of the running app and cron containers here, on every network they are on."""
    out: dict[str, int] = {}
    for container in _get_client().containers.list(filters={"status": "running"}):
        found = _NAME.match(container.name or "")
        if not found:
            continue
        app_id = int(found.group(1) or found.group(2))
        networks = (container.attrs.get("NetworkSettings") or {}).get("Networks") or {}
        for network in networks.values():
            if (network or {}).get("IPAddress"):
                out[network["IPAddress"]] = app_id
    return out


def attribute(counts: dict[str, int], before: dict[str, int], now: dict[str, int]) -> dict[int, int]:
    """{app id: bytes}: an address belongs to the app it was counted for (before), or else to the one that has it
    now (the first collect after a restart, when `before` is empty)."""
    out: dict[int, int] = {}
    for ip, n in counts.items():
        app_id = before.get(ip, now.get(ip))
        if app_id is not None and n > 0:
            out[app_id] = out.get(app_id, 0) + int(n)
    return out


def collect() -> dict[int, int]:
    """{app id: bytes} sent to public addresses since the last call. Raises QuotaError without the helper."""
    global _counted
    now = container_addresses()
    counts = quota.traffic(list(now))
    out = attribute(counts, _counted, now)
    _counted = now
    return out
