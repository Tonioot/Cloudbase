"""What a node tells the primary on its own, without being asked: the stats of its instances and the state (and
stats) of its databases, and what a database is doing before its container exists (an image pull takes minutes). Sent over the agent's websocket; the primary only stores it (nodes/reports.py).

Instances every TICK_SECONDS; databases (and what the apps sent to the internet) every DB_EVERY-th tick, with their usage every DB_STATS_EVERY-th of those
and the size of their data now and then (du is slow).
"""
import asyncio
import time
from typing import Awaitable, Callable

from containers import instances as containers_instances, stats_logs as containers_stats_logs
from containers import traffic as containers_traffic
from dbs import docker as dbs_docker, lifecycle as dbs_lifecycle
from agent.log import _agent_log

TICK_SECONDS = 5
DB_EVERY = 6                # databases every 30 s
DB_STATS_EVERY = 2          # their usage every minute
DB_DISK_EVERY = 20          # their data size every ten minutes


async def instance_stats() -> dict[str, dict]:
    """{instance id: stats} of the running instance containers here."""
    containers = [c for c in await asyncio.to_thread(containers_instances.list_instance_containers) if c["status"] == "running"]
    results = await asyncio.gather(*[asyncio.to_thread(containers_stats_logs.get_container_stats_by_name, c["name"])
                                     for c in containers])
    stamp = int(time.time() * 1000)
    return {str(c["instance_id"]): {**s, "timestamp": stamp} for c, s in zip(containers, results) if s}


async def report_instances(send: Callable[[dict], Awaitable[None]]) -> None:
    try:
        await send({"type": "database_progress", "progress": {str(k): v for k, v in dbs_docker.all_progress().items()}})
        await send({"type": "instance_stats", "stats": await instance_stats()})
    except Exception as e:
        _agent_log(f"[report] instance stats: {type(e).__name__}: {e}")


async def report_databases(db_round: int, send: Callable[[dict], Awaitable[None]]) -> None:
    try:
        databases = await asyncio.to_thread(dbs_lifecycle.report_all, db_round % DB_STATS_EVERY == 0, db_round % DB_DISK_EVERY == 0)
        await send({"type": "database_report", "databases": databases})
    except Exception as e:
        _agent_log(f"[report] databases: {type(e).__name__}: {e}")


_traffic: dict[int, int] = {}      # counted, not sent yet
_traffic_warned = False


async def report_traffic(send: Callable[[dict], Awaitable[None]]) -> None:
    """What the apps here sent to the public internet since the last report (containers/traffic.py). Kept until it
    went out: the firewall counted it once and will not count it again."""
    global _traffic_warned
    try:
        counts = await asyncio.to_thread(containers_traffic.collect)
    except Exception as e:
        if not _traffic_warned:
            _agent_log(f"[report] internet traffic is not counted: {type(e).__name__}: {e}")
            _traffic_warned = True
        return
    for app_id, n in counts.items():
        _traffic[app_id] = _traffic.get(app_id, 0) + n
    if not _traffic:
        return
    try:
        await send({"type": "traffic_report", "apps": {str(k): v for k, v in _traffic.items()}})
        _traffic.clear()
    except Exception as e:
        _agent_log(f"[report] internet traffic: {type(e).__name__}: {e}")


async def report_slow(db_round: int, send: Callable[[dict], Awaitable[None]]) -> None:
    """Every DB_EVERY-th tick: the databases, and the apps' internet traffic."""
    await report_databases(db_round, send)
    await report_traffic(send)


async def report_once(tick: int, send: Callable[[dict], Awaitable[None]]) -> None:
    """One round: the instances, and the databases when it is their turn."""
    await report_instances(send)
    db_round, offset = divmod(tick, DB_EVERY)
    if offset == 0:
        await report_slow(db_round, send)


async def report_forever(send: Callable[[dict], Awaitable[None]]) -> None:
    """For as long as the websocket is up. The instances on a fixed TICK_SECONDS clock (the time a round takes is not
    added to it); the databases beside them, so measuring them (du can take seconds) never holds the instances up."""
    loop = asyncio.get_running_loop()
    tick, next_at, databases = 0, loop.time(), None
    try:
        while True:
            db_round, offset = divmod(tick, DB_EVERY)
            if offset == 0 and (databases is None or databases.done()):
                databases = asyncio.create_task(report_slow(db_round, send))
            await report_instances(send)
            tick += 1
            next_at = max(next_at + TICK_SECONDS, loop.time())
            await asyncio.sleep(next_at - loop.time())
    finally:
        if databases is not None:
            databases.cancel()
