"""What the app containers on this machine send to the public internet, per app.

The root helper (scripts/cloudbase-quota) keeps one counter per container address in the firewall, for packets that
leave through a Cloudbase bridge towards a public address; a private destination (nginx, a database, another app or
node, the local network) is not counted. Every collect() reads and zeroes those counters and hands the helper the
addresses of the containers running now. Instances and cron jobs both count for their app. watch() does the same as
soon as a container starts, so also a cron job that is gone within seconds is counted.

Blocking: call from a thread. Used by the primary (workers/traffic.py) and by the agent (agent/report.py).
"""
import logging
import re
import threading
import time

from containers import quota
from containers.client import _get_client

log = logging.getLogger("cloudbase.traffic")

_NAME = re.compile(r"^cloudbase-(?:app-(\d+)-instance-\d+|cron-(\d+)-\d+)$")

_counted: dict[str, int] = {}     # address -> app id, of what the helper counts until the next read
_pending: dict[int, int] = {}     # app id -> bytes read and not handed out yet
_lock = threading.Lock()          # one read at a time: reading also zeroes the counters
_watcher: threading.Thread | None = None


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


def _read() -> None:
    """Read the counters into _pending and count the containers that run now. Call with _lock."""
    global _counted
    now = container_addresses()
    counts = quota.traffic(list(now))
    for app_id, n in attribute(counts, _counted, now).items():
        _pending[app_id] = _pending.get(app_id, 0) + n
    _counted = now


def collect() -> dict[int, int]:
    """{app id: bytes} sent to public addresses since the last call. Raises QuotaError without the helper."""
    with _lock:
        _read()
        out = dict(_pending)
        _pending.clear()
        return out


def started(name: str) -> bool:
    """A container started: when it is an app's, count it from now on. True when it is."""
    if not _NAME.match(name or ""):
        return False
    with _lock:
        _read()
    return True


def watch() -> None:
    """Until the process ends: follow Docker's "container started" events. collect() stays the safety net."""
    warned = False
    while True:
        try:
            for event in _get_client().events(decode=True, filters={"type": ["container"], "event": ["start"]}):
                started((((event or {}).get("Actor") or {}).get("Attributes") or {}).get("name"))
                warned = False
        except Exception as e:
            if not warned:
                log.warning("[traffic] New containers are only counted every round: %s", e)
                warned = True
        time.sleep(5)


def start_watching() -> None:
    global _watcher
    if _watcher is None or not _watcher.is_alive():
        _watcher = threading.Thread(target=watch, name="cloudbase-traffic-watch", daemon=True)
        _watcher.start()
