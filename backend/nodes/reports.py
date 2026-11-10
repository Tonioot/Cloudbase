"""What nodes send by themselves (agent/report.py): the stats of their instances and the state of their databases.

Nothing here asks a node anything; the primary only keeps the latest of each, and whoever needs it (the stats
collector, the database monitor) reads it. A report that is too old counts as no report: the node is gone or busy.
"""
import time

from sqlalchemy import select

from containers import processes as pm
from core.database import AsyncSessionLocal
from core.models import Instance

MAX_AGE_SECONDS = 30            # instance stats older than this are not used (a node sends them every 10 s)
DATABASE_MAX_AGE_SECONDS = 90   # database states: a node sends them every 30 s

DATABASE_STATS_MAX_AGE_SECONDS = 150   # usage: sent every minute

_databases: dict[int, tuple[float, dict]] = {}       # node id -> (when, {database id: {state, volume}})
_database_stats: dict[int, tuple[float, dict]] = {}  # database id -> (when, stats), from the last report that had them


async def store_instance_stats(node_id: int, stats: dict) -> None:
    """Keep the stats a node sent, for the instances that really are on that node."""
    ids = [int(k) for k in stats if str(k).isdigit()]
    if not ids:
        return
    async with AsyncSessionLocal() as db:
        mine = set((await db.execute(select(Instance.id).where(Instance.node_id == node_id, Instance.id.in_(ids)))).scalars())
    now = time.monotonic()
    for instance_id in mine:
        pm.set_instance_stats(instance_id, {**stats[str(instance_id)], "instance_id": instance_id, "received": now})


def instance_stats(instance_id: int) -> dict | None:
    """The latest stats a node sent for this instance, while they are recent."""
    frame = pm.get_instance_stats(instance_id)
    if frame and time.monotonic() - frame.get("received", float("-inf")) <= MAX_AGE_SECONDS:
        return {k: v for k, v in frame.items() if k not in ("received", "instance_id", "timestamp")}
    return None


def store_databases(node_id: int, databases: dict) -> None:
    now = time.monotonic()
    for db_id, info in (databases or {}).items():
        if info.get("stats"):
            previous = (_database_stats.get(int(db_id)) or (0, {}))[1]
            _database_stats[int(db_id)] = (now, {"disk_mb": previous.get("disk_mb"), **info["stats"]})   # the size is measured less often
    _databases[node_id] = (now, {k: {"state": v.get("state"), "volume": v.get("volume")} for k, v in (databases or {}).items()})


def databases(node_id: int) -> tuple[float, dict] | None:
    """(when, {database id: {state, volume}}) as the node last sent it, or None when it has not sent one lately."""
    when, report = _databases.get(node_id, (float("-inf"), None))
    return (when, report) if time.monotonic() - when <= DATABASE_MAX_AGE_SECONDS else None


def database_stats(database_id: int) -> dict | None:
    """The database's usage as its node last sent it, while recent."""
    when, stats = _database_stats.get(database_id, (float("-inf"), None))
    return stats if time.monotonic() - when <= DATABASE_STATS_MAX_AGE_SECONDS else None
