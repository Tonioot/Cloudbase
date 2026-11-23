"""The primary adds up what apps send to the public internet (apps/traffic.py), every INTERVAL seconds:
what its own app containers sent (containers/traffic.py) and what nginx sent to their visitors (nginx/traffic_logs.py).
Nodes count their own containers and report them over the websocket (agent/report.py)."""
import asyncio
import json
import logging
import os

from sqlalchemy import select

from apps import traffic as app_traffic
from containers import traffic as containers_traffic
from core.database import AsyncSessionLocal
from core.models import SystemConfig
from nginx import files as nginx_files, traffic_logs
from nginx.panel import _refresh_all_app_nginx
from nginx.paths import NGINX_SITES_DIR, TRAFFIC_LOG_DIR

log = logging.getLogger("cloudbase.traffic")

INTERVAL = 30
OFFSETS_KEY = "traffic.nginx_offsets"

_pending: dict[int, int] = {}      # counted on this machine, not stored yet (the database was busy)
_warned = False


def _configs_without_logging() -> bool:
    """App configs written before the visitors' traffic was logged (or before the log directory existed)."""
    if not os.path.isdir(TRAFFIC_LOG_DIR):
        return False
    try:
        for name in os.listdir(NGINX_SITES_DIR):
            if name.startswith("app-"):
                with open(os.path.join(NGINX_SITES_DIR, name), encoding="utf-8") as f:
                    content = f.read()
                if "proxy_pass" in content and "cloudbase_traffic_" not in content:
                    return True
    except OSError:
        return False
    return False


def _collect_internet() -> dict[int, int]:
    """Blocking. What the app containers here sent; nothing (said once) without the helper or Docker."""
    global _warned
    try:
        counts = containers_traffic.collect()
        _warned = False
        return counts
    except Exception as e:
        if not _warned:
            log.warning("[traffic] Apps' internet traffic is not counted: %s", e)
            _warned = True
        return {}


async def count_once() -> None:
    for app_id, n in (await asyncio.to_thread(_collect_internet)).items():
        _pending[app_id] = _pending.get(app_id, 0) + n
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(SystemConfig).where(SystemConfig.key == OFFSETS_KEY))).scalar_one_or_none()
        try:
            offsets = {str(k): int(v) for k, v in json.loads(row.value or "{}").items()} if row else {}
        except (ValueError, AttributeError):
            offsets = {}
        visitors, offsets, reopen = await asyncio.to_thread(traffic_logs.read_new, offsets)
        await app_traffic.add_in(db, "visitors", visitors)
        await app_traffic.add_in(db, "internet", dict(_pending))
        if row is None:
            db.add(SystemConfig(key=OFFSETS_KEY, value=json.dumps(offsets)))
        else:
            row.value = json.dumps(offsets)
        await db.commit()                  # the numbers and the offsets they came from, together
    _pending.clear()
    if reopen:
        await asyncio.to_thread(nginx_files.reopen_logs)


async def traffic_counter() -> None:
    try:
        if await asyncio.to_thread(_configs_without_logging):
            log.info("[traffic] Rewriting the apps' nginx configs to count what their visitors get")
            await _refresh_all_app_nginx()
    except Exception as e:
        log.warning("[traffic] Could not rewrite the apps' nginx configs: %s", e)
    while True:
        await asyncio.sleep(INTERVAL)
        try:
            await count_once()
        except Exception as e:
            log.warning("[traffic] Counting failed (tried again in %d s): %s", INTERVAL, e)
