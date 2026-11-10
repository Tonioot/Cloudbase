"""What a node tells the primary on its own, without being asked: the stats of its instances and the state (and
stats) of its databases. Sent over the agent's websocket; the primary only stores it (nodes/reports.py).

Instances: every FULL_SECONDS all of them; every TICK_SECONDS also those of the apps somebody is looking at
(the primary says which with a "watch" message, see watch()). Databases: every DB_SECONDS their state, with their
usage every DB_STATS_EVERY-th round and the size of their data now and then (du is slow).
"""
import asyncio
import time
from typing import Awaitable, Callable

from containers import instances as containers_instances, stats_logs as containers_stats_logs
from dbs import lifecycle as dbs_lifecycle
from agent.log import _agent_log

TICK_SECONDS = 2
FULL_SECONDS = 10
WATCH_SECONDS = 30
DB_SECONDS = 30
DB_STATS_EVERY = 2          # usage every minute
DB_DISK_EVERY = 20          # data size every ten minutes

_watched: dict[int, float] = {}          # app id -> until when somebody looks at it (monotonic)


def watch(app_id: int) -> None:
    """Somebody looks at this app's stats: send its instances every tick for a while."""
    _watched[int(app_id)] = time.monotonic() + WATCH_SECONDS


def watched_apps(now: float) -> set[int]:
    for app_id in [a for a, until in _watched.items() if until <= now]:
        del _watched[app_id]
    return set(_watched)


async def instance_stats(app_ids: set[int] | None) -> dict[str, dict]:
    """{instance id: stats} of the running instance containers here: all of them, or those of these apps."""
    containers = [c for c in await asyncio.to_thread(containers_instances.list_instance_containers)
                  if c["status"] == "running" and (app_ids is None or c["app_id"] in app_ids)]
    results = await asyncio.gather(*[asyncio.to_thread(containers_stats_logs.get_container_stats_by_name, c["name"])
                                     for c in containers])
    stamp = int(time.time() * 1000)
    return {str(c["instance_id"]): {**s, "timestamp": stamp} for c, s in zip(containers, results) if s}


async def report_once(tick: int, send: Callable[[dict], Awaitable[None]]) -> None:
    """One round: what is due at this tick."""
    full = tick % (FULL_SECONDS // TICK_SECONDS) == 0
    watched = watched_apps(time.monotonic())
    if full or watched:
        try:
            await send({"type": "instance_stats", "stats": await instance_stats(None if full else watched)})
        except Exception as e:
            _agent_log(f"[report] instance stats: {type(e).__name__}: {e}")
    db_round, offset = divmod(tick, DB_SECONDS // TICK_SECONDS)
    if offset == 0:
        try:
            databases = await asyncio.to_thread(dbs_lifecycle.report_all, db_round % DB_STATS_EVERY == 0, db_round % DB_DISK_EVERY == 0)
            await send({"type": "database_report", "databases": databases})
        except Exception as e:
            _agent_log(f"[report] databases: {type(e).__name__}: {e}")


async def report_forever(send: Callable[[dict], Awaitable[None]]) -> None:
    """For as long as the websocket is up."""
    tick = 0
    while True:
        await report_once(tick, send)
        tick += 1
        await asyncio.sleep(TICK_SECONDS)
