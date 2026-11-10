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
from core.models import Application, Node as _Node, Instance as _AR, OverviewHistory, NodeMetricsHistory, StatsHistory, Instance as _AppInstance, Application as _App
from containers import processes as pm
from containers import naming as containers_naming
from containers import stats_logs as containers_stats_logs
from nodes import commands as node_commands
from nodes.state import _node_ws_connections
from apps.runtime import has_running_instance
from apps.stream import _aggregate_frames
from core.history import _HISTORY_RETENTION_DAYS, _OVERVIEW_SAMPLE_SECONDS

log = logging.getLogger("cloudbase.main")


# ── Background stats collector ────────────────────────────────────────────────
# Every Docker stats call takes about a second and runs in a thread. They used to run for every instance every 2 s,
# twice (once for the instance, once for the app), whether anyone looked or not.
IDLE_SAMPLE_SECONDS = 15      # nobody is looking at the app: this often is enough for history and the instances table
STATS_CONCURRENCY = 12        # at most this many Docker stats calls at once, so other threaded work is not starved
_last_sample: dict[int, float] = {}


def _wants_sample(app_id: int, now: float, last_sample: dict) -> bool:
    """Every tick while someone watches the app (stats tab, instances tab); otherwise every IDLE_SAMPLE_SECONDS."""
    return pm.is_watched(app_id, now) or now - last_sample.get(app_id, float("-inf")) >= IDLE_SAMPLE_SECONDS - 0.5


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


async def collect_local_stats_once(now: float | None = None) -> int:
    """One round: sample the running local instances of every app that is due, once each. Returns the number of
    Docker stats calls made."""
    now = _time.monotonic() if now is None else now
    async with AsyncSessionLocal() as db:
        apps = (await db.execute(select(Application).where(has_running_instance()))).scalars().all()
        local_node = (await db.execute(select(_Node).where(_Node.is_local == True))).scalar_one_or_none()  # noqa: E712
        local_node_id = local_node.id if local_node else None
        running_instances = (await db.execute(
            select(_AppInstance).where(
                _AppInstance.status == "running",
                _AppInstance.node_id == local_node_id,
            )
        )).scalars().all()
    # The session is closed here: Docker is asked with no database lock held.

    instances_by_app: dict[int, list] = {}
    for r in running_instances:
        instances_by_app.setdefault(r.app_id, []).append(r)
    return await sample_due_apps(apps, instances_by_app, now)


async def sample_due_apps(apps, instances_by_app: dict[int, list], now: float, last_sample: dict | None = None) -> int:
    """Ask Docker about the running instances of the apps that are due. Returns the number of calls made."""
    last_sample = _last_sample if last_sample is None else last_sample
    due = [a for a in apps if instances_by_app.get(a.id) and _wants_sample(a.id, now, last_sample)]   # only this server's instances are in it
    gate = asyncio.Semaphore(STATS_CONCURRENCY)
    calls = 0

    async def sample(instance):
        nonlocal calls
        async with gate:
            calls += 1
            return await asyncio.to_thread(
                containers_stats_logs.get_container_stats_by_name,
                containers_naming.instance_container_name(instance.app_id, instance.id))

    async def one_app(a):
        try:
            instances = instances_by_app[a.id]
            results = await asyncio.gather(*[sample(r) for r in instances])
            last_sample[a.id] = now
            stats_list = []
            stamp = int(_time.time() * 1000)
            for instance, s in zip(instances, results):
                if not s:
                    continue
                stats_list.append(s)
                frame = {"instance_id": instance.id, "timestamp": stamp, **s}
                pm.set_instance_stats(instance.id, frame)
                pm._push_stat(a.id, frame)
            if not stats_list:
                return
            mem = psutil.virtual_memory()
            data = {
                "status": "running",
                "docker": True,
                "timestamp": stamp,
                **_aggregate(stats_list),
                "system_cpu_percent": psutil.cpu_percent(interval=None),
                "system_memory_total_mb": round(mem.total / 1024 / 1024),
                "system_memory_used_mb":  round(mem.used  / 1024 / 1024),
                "system_memory_percent":  mem.percent,
            }
            pm._stats_history.setdefault(a.id, deque(maxlen=60)).append(data)
            pm._push_stat(a.id, data)
        except Exception:
            pass

    await asyncio.gather(*[one_app(a) for a in due])
    for app_id in [i for i in last_sample if i not in instances_by_app]:
        del last_sample[app_id]                                    # apps that no longer run here
    return calls


async def _stats_collector():
    """Sample the instances on this server: every 2 s for apps someone is watching, every 15 s for the rest."""
    await asyncio.sleep(4)
    while True:
        try:
            await collect_local_stats_once()
        except asyncio.CancelledError:
            return
        except Exception:
            pass
        await asyncio.sleep(2)


# ── Remote instance stats poller ───────────────────────────────────────────────
async def _remote_instance_stats_poller():
    """Poll remote nodes every 15s to keep per-instance stats fresh for the instances tab.

    Only fires when no stats WebSocket relay is already streaming for a given app
    (the relay in stats.py feeds _instance_stats in real-time when the stats tab is open).
    Uses one get_stats command per (node, app) pair — not per instance — to keep load low.
    """
    await asyncio.sleep(20)
    while True:
        try:

            async with AsyncSessionLocal() as db:
                local_node_result = await db.execute(select(_Node).where(_Node.is_local == True))
                local_node_obj = local_node_result.scalar_one_or_none()
                local_node_id = local_node_obj.id if local_node_obj else None

                rep_result = await db.execute(
                    select(_AR).where(
                        _AR.status.in_(["running", "starting"]),
                        _AR.node_id.isnot(None),
                    )
                )
                all_running_instances = rep_result.scalars().all()

            remote_instances = [r for r in all_running_instances if r.node_id != local_node_id]

            if not remote_instances:
                await asyncio.sleep(15)
                continue

            # Group by (node_id, app_id) — one command per pair
            from collections import defaultdict
            groups: dict[tuple[int, int], list] = defaultdict(list)
            for r in remote_instances:
                groups[(r.node_id, r.app_id)].append(r)

            async def _poll_group(node_id: int, app_id: int, instances: list):
                # Skip if no agent WS — command would just queue forever
                if node_id not in _node_ws_connections:
                    return
                try:
                    async with AsyncSessionLocal() as db:
                        app_obj = await db.get(_App, app_id)
                    if not app_obj:
                        return
                    app_name = app_obj.name
                    done = await node_commands.ask(node_id, "get_stats", {"app_id": app_id, "app_name": app_name},
                                                   app_id=app_id, reuse=True)
                    _rlog = logging.getLogger("cloudbase.remote_stats")
                    _rlog.debug("poll node=%d app=%d cmd=%d status=%s", node_id, app_id, done.id, done.status)
                    if done.status == "done" and done.result:
                        s = done.result
                        _rlog.info("poll node=%d app=%d result keys=%s cpu=%s",
                                   node_id, app_id, list(s.keys()), s.get("cpu_percent"))
                        if s.get("cpu_percent") is not None:
                            snap = {
                                k: v for k, v in s.items()
                                if k not in ("status", "remote", "docker",
                                             "system_cpu_percent", "system_memory_total_mb",
                                             "system_memory_used_mb", "system_memory_percent")
                            }
                            snap["timestamp"] = int(_time.time() * 1000)
                            for instance in instances:
                                pm.set_instance_stats(instance.id, {"instance_id": instance.id, **snap})
                            app_frames.setdefault(app_id, []).append(dict(snap))
                            _rlog.info("poll stored stats for %d instances on node=%d", len(instances), node_id)
                        elif s.get("status") == "stopped":
                            _rlog.info("poll node=%d app=%d temporarily stopped during restart", node_id, app_id)
                            # Fallback to per-instance stats to avoid losing metrics when
                            # app-level aggregate briefly reports stopped.
                            for instance in instances:
                                try:
                                    rdone = await node_commands.ask(
                                        node_id, "get_instance_stats",
                                        {"app_id": app_id, "app_name": app_name, "instance_id": instance.id},
                                        app_id=app_id, timeout=12)
                                    if rdone.status == "done" and rdone.result:
                                        rs = rdone.result
                                        if rs.get("cpu_percent") is not None:
                                            pm.set_instance_stats(instance.id, {"instance_id": instance.id, **rs})
                                except Exception:
                                    continue
                except Exception as _e:
                    logging.getLogger("cloudbase.remote_stats").warning(
                        "remote stats poll failed node=%d app=%d: %s", node_id, app_id, _e)

            app_frames: dict[int, list[dict]] = {}
            await asyncio.gather(*[_poll_group(nid, aid, reps) for (nid, aid), reps in groups.items()])

            # The local stats collector skips apps on remote nodes, so without this
            # their history (live charts on open + the DB history writer) would only
            # fill while someone has the app's stats open. One frame per app,
            # merged over all nodes it runs on.
            for app_id, frames in app_frames.items():
                merged = _aggregate_frames(frames)
                merged.update({"status": "running", "docker": True, "timestamp": int(_time.time() * 1000)})
                pm._stats_history.setdefault(app_id, deque(maxlen=60)).append(merged)

        except asyncio.CancelledError:
            return
        except Exception:
            pass
        await asyncio.sleep(15)


async def _overview_sampler():
    import datetime as _dt
    from sqlalchemy import delete as _delete
    await asyncio.sleep(5)
    rounds = 0
    while True:
        try:
            async with AsyncSessionLocal() as db:
                apps = (await db.execute(select(Application))).scalars().all()
                instances = (await db.execute(select(_AR))).scalars().all()
                nodes = (await db.execute(select(_Node))).scalars().all()

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
