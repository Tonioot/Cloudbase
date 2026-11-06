import asyncio
import logging
import time
from datetime import timedelta, datetime
from sqlalchemy import select
from dbs import engines as db_engines, links as db_links, manager as db_manager, proxy as db_proxy
from core.database import AsyncSessionLocal
from core.models import DatabaseStatsHistory, Database, Node
from dbs.ops import _node_reachable, _run_on_node, _set_status, _spawn, _start_operation, _state_to_status
from dbs.provision import _data_dir
from dbs.relay import open_db_stream, push_proxies
from dbs.state import HEAL_BACKOFF_SECONDS, MONITOR_INTERVAL_SECONDS, _busy, _last_heal, _update_info

log = logging.getLogger("cloudbase.databases")


# ── Monitor ───────────────────────────────────────────────────────────────────

async def _fetch_states(node: Node, ids: list[int]) -> dict[str, dict]:
    result = await _run_on_node(
        node,
        lambda: {"states": db_manager.states(ids)},
        "db_states",
        {"database_ids": ids},
        timeout=30,
    )
    return result.get("states") or {}


async def _monitor_tick() -> None:
    async with AsyncSessionLocal() as db:
        databases = (await db.execute(select(Database))).scalars().all()
        nodes = {n.id: n for n in (await db.execute(select(Node))).scalars().all()}

    by_node: dict[int, list[Database]] = {}
    for d in databases:
        by_node.setdefault(d.node_id, []).append(d)

    for node_id, items in by_node.items():
        node = nodes.get(node_id)
        idle = [d for d in items if d.id not in _busy]
        if not idle:
            continue
        if node is None or not _node_reachable(node):
            for d in idle:
                if d.status != "node_offline":
                    await _set_status(d.id, "node_offline", d.last_error)
            continue
        try:
            states = await _fetch_states(node, [d.id for d in idle])
        except Exception as e:
            log.debug("[db-monitor] state check on node %s failed: %s", node.name, e)
            continue

        for d in idle:
            if d.id in _busy:
                continue
            info = states.get(str(d.id)) or {}
            state = info.get("state", "missing")

            if d.desired_state == "stopped":
                if state in ("stopped", "missing") and d.status != "stopped":
                    await _set_status(d.id, "stopped")
                elif state not in ("stopped", "missing"):
                    status, error = _state_to_status(state)
                    await _set_status(d.id, status, error)
                continue

            if state == "missing" and d.initialized_at is not None and not info.get("volume", True):
                message = (f"Data volume {db_manager.volume_name(d.id)} is missing on node '{node.name}'. "
                           "Cloudbase will not create an empty database in its place.")
                if d.status != "error" or d.last_error != message:
                    await _set_status(d.id, "error", message)
                continue

            if state in ("stopped", "missing"):
                # Should be running but is not: start it again, with a backoff.
                now = time.monotonic()
                if now - _last_heal.get(d.id, 0) < HEAL_BACKOFF_SECONDS:
                    continue
                _last_heal[d.id] = now
                log.info("[db-monitor] database %s (%s) is %s, starting it", d.name, d.id, state)
                db_id = d.id
                _spawn(db_id, lambda db_id=db_id: _start_operation(db_id, "ensure"))
                continue

            status, error = _state_to_status(state)
            if status != d.status or (error or None) != (d.last_error or None) or (status == "running" and d.initialized_at is None):
                await _set_status(d.id, status, error)


async def database_monitor() -> None:
    """Keep database statuses in sync with Docker on every node (primary only)."""
    async with AsyncSessionLocal() as db:
        await db_links.refresh(db)
    db_proxy.configure(asyncio.get_running_loop(), open_db_stream)
    await asyncio.sleep(10)
    while True:
        try:
            await _monitor_tick()
            await push_proxies()
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.warning("[db-monitor] error: %s", e)
        await asyncio.sleep(MONITOR_INTERVAL_SECONDS)


# ── Usage history ─────────────────────────────────────────────────────────────

STATS_INTERVAL_SECONDS = 60


STATS_DISK_EVERY = 10          # measure the data size every 10th sample (du is slow)


STATS_RETENTION_DAYS = 7


async def _stats_tick(with_disk: bool) -> None:
    async with AsyncSessionLocal() as db:
        databases = (await db.execute(
            select(Database).where(Database.status.in_(["running", "unhealthy", "starting"]))
        )).scalars().all()
        nodes = {n.id: n for n in (await db.execute(select(Node))).scalars().all()}
    by_node: dict[int, list[dict]] = {}
    for d in databases:
        by_node.setdefault(d.node_id, []).append(
            {"database_id": d.id, "data_dir": _data_dir(d) if with_disk else None})
    now = datetime.utcnow()
    rows = []
    for node_id, items in by_node.items():
        node = nodes.get(node_id)
        if node is None or not _node_reachable(node):
            continue
        try:
            result = await _run_on_node(node, lambda items=items: {"stats": db_manager.stats_many(items)},
                                        "db_stats_many", {"items": items}, timeout=60)
        except Exception as e:
            log.debug("[db-stats] node %s: %s", node.name, e)
            continue
        for db_id, s in (result.get("stats") or {}).items():
            rows.append(DatabaseStatsHistory(
                database_id=int(db_id), timestamp=now,
                cpu_percent=s.get("cpu_percent"), memory_mb=s.get("memory_mb"), disk_mb=s.get("disk_mb"),
            ))
    if rows:
        async with AsyncSessionLocal() as db:
            db.add_all(rows)
            await db.commit()


async def database_stats_sampler() -> None:
    """Sample database usage every minute for the history charts (primary only)."""
    from sqlalchemy import delete as _delete
    await asyncio.sleep(45)
    tick = 0
    while True:
        try:
            await _stats_tick(with_disk=tick % STATS_DISK_EVERY == 0)
            if tick % 60 == 0:
                async with AsyncSessionLocal() as db:
                    await db.execute(_delete(DatabaseStatsHistory).where(
                        DatabaseStatsHistory.timestamp < datetime.utcnow() - timedelta(days=STATS_RETENTION_DAYS)))
                    await db.commit()
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.warning("[db-stats] error: %s", e)
        tick += 1
        await asyncio.sleep(STATS_INTERVAL_SECONDS)


# ── Update checks ─────────────────────────────────────────────────────────────

UPDATE_CHECK_INTERVAL_SECONDS = 12 * 3600


async def _check_updates_on(node: Node, databases: list) -> None:
    items = [{"database_id": d.id, "ref": db_engines.get_engine(d.engine).image_ref(d.version)} for d in databases]
    result = await _run_on_node(node, lambda: {"updates": db_manager.check_updates(items)},
                                "db_check_updates", {"items": items}, timeout=120)
    now = datetime.utcnow().isoformat()
    for db_id, info in (result.get("updates") or {}).items():
        _update_info[int(db_id)] = {**info, "checked_at": now}


async def database_update_checker() -> None:
    """Look for newer patch releases twice a day (primary only)."""
    await asyncio.sleep(300)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                databases = (await db.execute(
                    select(Database).where(Database.status.in_(["running", "unhealthy"]))
                )).scalars().all()
                nodes = {n.id: n for n in (await db.execute(select(Node))).scalars().all()}
            by_node: dict[int, list] = {}
            for d in databases:
                by_node.setdefault(d.node_id, []).append(d)
            for node_id, items in by_node.items():
                node = nodes.get(node_id)
                if node is not None and _node_reachable(node):
                    try:
                        await _check_updates_on(node, items)
                    except Exception as e:
                        log.debug("[db-updates] node %s: %s", node.name, e)
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.warning("[db-updates] error: %s", e)
        await asyncio.sleep(UPDATE_CHECK_INTERVAL_SECONDS)
