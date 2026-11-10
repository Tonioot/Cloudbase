import asyncio
import json
import logging
import time as _time
from collections import deque
import psutil
from core import sysmetrics
from nodes import metrics as node_metrics
from sqlalchemy import select
from core.database import AsyncSessionLocal
from core.models import Application, Instance, Node, NodeMetricsHistory, OverviewHistory, StatsHistory
from containers import processes as pm
from containers import naming as containers_naming
from containers import stats_logs as containers_stats_logs
from nodes import reports as node_reports
from apps.runtime import has_running_instance
from core.history import _HISTORY_RETENTION_DAYS, _OVERVIEW_SAMPLE_SECONDS

log = logging.getLogger("cloudbase.main")


# ── Background stats collector ────────────────────────────────────────────────
# Every SAMPLE_SECONDS one frame per app over all its instances: Docker for the ones here (a stats call takes about a
# second and runs in a thread), what their node sent last for the others (agent/report.py does the same every 5 s).
SAMPLE_SECONDS = 5
STATS_CONCURRENCY = 12        # at most this many Docker stats calls at once, so other threaded work is not starved


def _aggregate(stats_list: list[dict]) -> dict:
    n = len(stats_list)
    return {
        "cpu_percent":    round(sum(s.get("cpu_percent",    0) for s in stats_list) / n, 2),
        "memory_mb":      round(sum(s.get("memory_mb",      0) for s in stats_list), 2),
        "memory_vms_mb":  round(sum(s.get("memory_vms_mb",  0) for s in stats_list), 2),
        "net_rx_mb":      round(sum(s.get("net_rx_mb",      0) for s in stats_list), 2),
        "net_tx_mb":      round(sum(s.get("net_tx_mb",      0) for s in stats_list), 2),
        "disk_read_mb":   round(sum(s.get("disk_read_mb",   0) for s in stats_list), 2),
        "disk_write_mb":  round(sum(s.get("disk_write_mb",  0) for s in stats_list), 2),
        "uptime_seconds": max((s.get("uptime_seconds", 0) for s in stats_list), default=0),
    }


async def collect_stats_once() -> int:
    """One round: every running instance of every app, wherever it runs, once each. Returns the number sampled."""
    async with AsyncSessionLocal() as db:
        apps = (await db.execute(select(Application).where(has_running_instance()))).scalars().all()
        local_node = (await db.execute(select(Node).where(Node.is_local == True))).scalar_one_or_none()  # noqa: E712
        local_node_id = local_node.id if local_node else None
        running = (await db.execute(select(Instance).where(Instance.status == "running"))).scalars().all()
    # The session is closed here: Docker and the nodes are asked with no database lock held.

    instances_by_app: dict[int, list] = {}
    for r in running:
        instances_by_app.setdefault(r.app_id, []).append(r)
    return await sample_apps(apps, instances_by_app, local_node_id)


async def sample_apps(apps, instances_by_app: dict[int, list], local_node_id: int | None = None) -> int:
    """Sample the running instances of these apps (Docker here; on another node: what its agent sent last) and make
    one frame per app out of all of them. Returns the number of instances sampled."""
    gate = asyncio.Semaphore(STATS_CONCURRENCY)
    calls = 0

    async def sample(instance):
        nonlocal calls
        async with gate:
            calls += 1
            if instance.node_id != local_node_id:
                return node_reports.instance_stats(instance.id)      # sent by its node (agent/report.py)
            return await asyncio.to_thread(
                containers_stats_logs.get_container_stats_by_name,
                containers_naming.instance_container_name(instance.app_id, instance.id))

    async def one_app(a):
        try:
            instances = instances_by_app[a.id]
            results = await asyncio.gather(*[sample(r) for r in instances])
            stats_list = []
            stamp = int(_time.time() * 1000)
            for instance, s in zip(instances, results):
                if not s:
                    continue
                stats_list.append(s)
                if instance.node_id == local_node_id:           # (those of other nodes are kept as their node sent them)
                    pm.set_instance_stats(instance.id, {"instance_id": instance.id, "timestamp": stamp, **s})   # the instances tab
            if not stats_list:
                return
            mem = psutil.virtual_memory()
            data = {
                "status": "running",
                "docker": True,
                "timestamp": stamp,
                **_aggregate(stats_list),
                "_instance_count": len(stats_list),
                "system_cpu_percent": psutil.cpu_percent(interval=None),
                "system_memory_total_mb": round(mem.total / 1024 / 1024),
                "system_memory_used_mb":  round(mem.used  / 1024 / 1024),
                "system_memory_percent":  mem.percent,
            }
            pm._stats_history.setdefault(a.id, deque(maxlen=60)).append(data)
            pm._push_stat(a.id, data)
        except Exception:
            pass

    await asyncio.gather(*[one_app(a) for a in apps if instances_by_app.get(a.id)])
    return calls


async def _stats_collector():
    """Sample every running instance every SAMPLE_SECONDS."""
    await asyncio.sleep(4)
    while True:
        try:
            await collect_stats_once()
        except asyncio.CancelledError:
            return
        except Exception:
            pass
        await asyncio.sleep(SAMPLE_SECONDS)


async def _overview_sampler():
    import datetime as _dt
    from sqlalchemy import delete as _delete
    await asyncio.sleep(5)
    rounds = 0
    while True:
        try:
            async with AsyncSessionLocal() as db:
                apps = (await db.execute(select(Application))).scalars().all()
                instances = (await db.execute(select(Instance))).scalars().all()
                nodes = (await db.execute(select(Node))).scalars().all()

                now = _dt.datetime.utcnow()
                online = [n for n in nodes if n.status == "online"]
                # cpu_percent(interval=None) measures since the previous call — read it once per round
                local_cpu = psutil.cpu_percent(interval=None)

                cpu_vals: list[float] = []
                mem_used = mem_total = 0.0
                for n in online:
                    if n.is_local:
                        vm = psutil.virtual_memory()
                        readings = {**sysmetrics.collect(), "cpu_percent": local_cpu}
                        cpu, mem = local_cpu, vm.percent
                        mem_total += vm.total / 1024 / 1024
                        mem_used += vm.used / 1024 / 1024
                    else:
                        readings = node_metrics.readings_of(n)
                        cpu, mem = n.node_cpu_percent, n.node_memory_percent
                        try:
                            total = (json.loads(n.metadata_json or "{}") or {}).get("ram_total_mb")
                        except Exception:
                            total = None
                        if total and mem is not None:
                            mem_total += total
                            mem_used += total * mem / 100
                    if cpu is not None:
                        cpu_vals.append(cpu)
                    db.add(node_metrics.history_row(n.id, now, readings))

                db.add(OverviewHistory(
                    timestamp=now,
                    apps_running=len({r.app_id for r in instances if r.status == "running"}),
                    apps_total=len(apps),
                    instances=sum(1 for r in instances if r.status == "running"),
                    cpu_percent=round(sum(cpu_vals) / len(cpu_vals), 1) if cpu_vals else None,
                    mem_used_mb=round(mem_used) if mem_total else None,
                    mem_total_mb=round(mem_total) if mem_total else None,
                    nodes_online=len(online),
                    nodes_total=len(nodes),
                ))

                rounds += 1
                if rounds % 120 == 1:   # about once an hour (and at startup)
                    cutoff = now - _dt.timedelta(days=_HISTORY_RETENTION_DAYS)
                    await db.execute(_delete(OverviewHistory).where(OverviewHistory.timestamp < cutoff))
                    await db.execute(_delete(NodeMetricsHistory).where(NodeMetricsHistory.timestamp < cutoff))
                await db.commit()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.debug(f"overview sampler failed: {exc}")
        await asyncio.sleep(_OVERVIEW_SAMPLE_SECONDS)


# ── Historical stats writer ───────────────────────────────────────────────────
async def _stats_history_writer():
    """Every 30s write averaged stats from the in-memory deque to the DB for long-term history.

    Works for both local and remote apps — remote apps buffer their stats into pm._stats_history
    via the stats WebSocket relay in routers/stats.py, so we just write whatever has accumulated.
    """
    import datetime as _dt
    from sqlalchemy import delete as _delete
    await asyncio.sleep(30)
    _cleanup_counter = 0
    while True:
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(Application).where(has_running_instance())
                )
                apps = result.scalars().all()
                for a in apps:
                    recent = pm.get_recent_stats(a.id)
                    if not recent:
                        continue
                    # Skip per-instance frames (they have instance_id, not cpu_percent at top level)
                    agg = [s for s in recent if "cpu_percent" in s]
                    if not agg:
                        continue
                    window = agg[-15:]
                    n = len(window)
                    avg_cpu  = sum(s.get("cpu_percent", 0) for s in window) / n
                    avg_mem  = sum(s.get("memory_mb",   0) for s in window) / n
                    avg_net  = sum((s.get("net_rx_mb",  0) or 0) + (s.get("net_tx_mb",   0) or 0) for s in window) / n
                    avg_disk = sum((s.get("disk_read_mb",0) or 0) + (s.get("disk_write_mb",0) or 0) for s in window) / n
                    db.add(StatsHistory(
                        app_id=a.id,
                        timestamp=_dt.datetime.utcnow(),
                        cpu_percent=round(avg_cpu, 2),
                        memory_mb=round(avg_mem, 2),
                        net_mb=round(avg_net, 2),
                        disk_mb=round(avg_disk, 2),
                    ))
                _cleanup_counter += 1
                if _cleanup_counter >= 120:
                    _cleanup_counter = 0
                    cutoff = _dt.datetime.utcnow() - _dt.timedelta(days=7)
                    await db.execute(_delete(StatsHistory).where(StatsHistory.timestamp < cutoff))
                await db.commit()
        except asyncio.CancelledError:
            return
        except Exception:
            pass
        await asyncio.sleep(30)
