import asyncio
import json
import logging
import time as _time
from collections import deque
import psutil
from sqlalchemy import select
from core.database import AsyncSessionLocal
from core.models import Application, Node as _Node, ApplicationReplica as _AR, OverviewHistory, NodeMetricsHistory, StatsHistory, ApplicationReplica as _AppReplica, Application as _App
from containers import processes as pm
from containers import naming as containers_naming
from containers import stats_logs as containers_stats_logs
from nodes.commands import wait_for_node_command, queue_node_command
from nodes.state import _node_ws_connections
from routers.stats import _aggregate_frames
from core.history import _HISTORY_RETENTION_DAYS, _OVERVIEW_SAMPLE_SECONDS

log = logging.getLogger("cloudbase.main")


# ── Background stats collector ────────────────────────────────────────────────
async def _stats_collector():
    """Collect process stats for all running apps every 2 s, push to subscribers."""
    await asyncio.sleep(4)
    while True:
        try:
            # Short read — session closed before any Docker/thread work begins.
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(Application).where(Application.status == "running")
                )
                apps = result.scalars().all()
                local_node_result = await db.execute(
                    select(_Node).where(_Node.is_local == True)
                )
                local_node_obj = local_node_result.scalar_one_or_none()
                local_node_id = local_node_obj.id if local_node_obj else None
                rep_result = await db.execute(
                    select(_AppReplica).where(
                        _AppReplica.status == "running",
                        (_AppReplica.node_id == local_node_id) | (_AppReplica.node_id.is_(None)),
                    )
                )
                running_replicas = rep_result.scalars().all()
            # Session is now closed — Docker stats collection happens below with no lock held.

            replicas_by_app: dict[int, list] = {}
            for _r in running_replicas:
                replicas_by_app.setdefault(_r.app_id, []).append(_r)

            async def _one(a):
                try:
                    if a.node_id and a.node_id != local_node_id:
                        return  # remote node apps stream their own stats via the agent
                    import time as _time
                    timestamp = int(_time.time() * 1000)  # milliseconds
                    app_replicas = replicas_by_app.get(a.id, [])
                    if app_replicas:
                        # Instance-based: aggregate stats from all running replicas
                        stats_list = await asyncio.gather(*[
                            asyncio.to_thread(
                                containers_stats_logs.get_container_stats_by_name,
                                containers_naming.replica_container_name(a.id, r.id),
                            )
                            for r in app_replicas
                        ])
                        stats_list = [s for s in stats_list if s]
                        if not stats_list:
                            return
                        n = len(stats_list)
                        s = {
                            "cpu_percent":    round(sum(s.get("cpu_percent",    0) for s in stats_list) / n, 2),
                            "memory_mb":      round(sum(s.get("memory_mb",      0) for s in stats_list), 2),
                            "memory_vms_mb":  round(sum(s.get("memory_vms_mb",  0) for s in stats_list), 2),
                            "net_rx_mb":      round(sum(s.get("net_rx_mb",      0) for s in stats_list), 2),
                            "net_tx_mb":      round(sum(s.get("net_tx_mb",      0) for s in stats_list), 2),
                            "disk_read_mb":   round(sum(s.get("disk_read_mb",   0) for s in stats_list), 2),
                            "disk_write_mb":  round(sum(s.get("disk_write_mb",  0) for s in stats_list), 2),
                            "uptime_seconds": max((s.get("uptime_seconds", 0) for s in stats_list), default=0),
                        }
                    else:
                        # Legacy single-container model
                        s = await asyncio.to_thread(pm.get_docker_stats, a.id)
                        if not s:
                            return
                    mem = psutil.virtual_memory()
                    data = {
                        "status": "running",
                        "pid": None,
                        "docker": True,
                        "timestamp": timestamp,
                        **s,
                        "system_cpu_percent": psutil.cpu_percent(interval=None),
                        "system_memory_total_mb": round(mem.total / 1024 / 1024),
                        "system_memory_used_mb":  round(mem.used  / 1024 / 1024),
                        "system_memory_percent":  mem.percent,
                    }
                    pm._stats_history.setdefault(a.id, deque(maxlen=60)).append(data)
                    pm._push_stat(a.id, data)
                except Exception:
                    pass

            # Collect all apps concurrently — cpu_percent(interval=0.5) runs in threads
            await asyncio.gather(*[_one(a) for a in apps])

            # Per-replica stats: store latest snapshot for local replicas
            async def _one_replica(replica):
                try:
                    cname = containers_naming.replica_container_name(replica.app_id, replica.id)
                    s = await asyncio.to_thread(containers_stats_logs.get_container_stats_by_name, cname)
                    if not s:
                        return
                    timestamp = int(_time.time() * 1000)
                    data = {
                        "replica_id": replica.id,
                        "timestamp": timestamp,
                        **s,
                    }
                    pm.set_replica_stats(replica.id, data)
                    pm._push_stat(replica.app_id, data)
                except Exception:
                    pass

            await asyncio.gather(*[_one_replica(r) for r in running_replicas])

        except asyncio.CancelledError:
            return
        except Exception:
            pass
        await asyncio.sleep(2)


# ── Remote replica stats poller ───────────────────────────────────────────────
async def _remote_replica_stats_poller():
    """Poll remote nodes every 15s to keep per-replica stats fresh for the instances tab.

    Only fires when no stats WebSocket relay is already streaming for a given app
    (the relay in stats.py feeds _replica_stats in real-time when the stats tab is open).
    Uses one get_stats command per (node, app) pair — not per replica — to keep load low.
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
                all_running_replicas = rep_result.scalars().all()

            remote_replicas = [r for r in all_running_replicas if r.node_id != local_node_id]
            local_replicas  = [r for r in all_running_replicas if r.node_id == local_node_id]

            # Poll local replica stats in-process (no command queue needed)
            if local_replicas:
                import time as _time

                async def _poll_local_replica(r):
                    try:
                        cname = containers_naming.replica_container_name(r.app_id, r.id)
                        snap = await asyncio.to_thread(containers_stats_logs.get_container_stats_by_name, cname)
                        if snap and snap.get("cpu_percent") is not None:
                            snap["timestamp"] = int(_time.time() * 1000)
                            pm.set_replica_stats(r.id, {"replica_id": r.id, **snap})
                    except Exception:
                        pass

                await asyncio.gather(*[_poll_local_replica(r) for r in local_replicas])

            if not remote_replicas:
                await asyncio.sleep(15)
                continue

            # Group by (node_id, app_id) — one command per pair
            from collections import defaultdict
            groups: dict[tuple[int, int], list] = defaultdict(list)
            for r in remote_replicas:
                groups[(r.node_id, r.app_id)].append(r)

            async def _poll_group(node_id: int, app_id: int, replicas: list):
                # Skip if no agent WS — command would just queue forever
                if node_id not in _node_ws_connections:
                    return
                try:
                    async with AsyncSessionLocal() as db:
                        app_r = await db.execute(select(_App).where(_App.id == app_id))
                        app_obj = app_r.scalar_one_or_none()
                        if not app_obj:
                            return
                        app_name = app_obj.name
                        cmd = await queue_node_command(
                            db,
                            node_id=node_id,
                            app_id=app_id,
                            command_type="get_stats",
                            payload={"app_id": app_id, "app_name": app_name},
                            allow_existing_inflight=True,
                        )
                        cmd_id = cmd.id
                    # wait_for_node_command uses its own sessions internally
                    async with AsyncSessionLocal() as wait_db:
                        done = await wait_for_node_command(wait_db, cmd_id, timeout_seconds=20)
                    _rlog = logging.getLogger("cloudbase.remote_stats")
                    _rlog.info("poll node=%d app=%d cmd=%d status=%s result_len=%s",
                               node_id, app_id, cmd_id, done.status,
                               len(done.result) if done.result else 0)
                    if done.status == "done" and done.result:
                        s = json.loads(done.result)
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
                            for replica in replicas:
                                pm.set_replica_stats(replica.id, {"replica_id": replica.id, **snap})
                            app_frames.setdefault(app_id, []).append(dict(snap))
                            _rlog.info("poll stored stats for %d replicas on node=%d", len(replicas), node_id)
                        elif s.get("status") == "stopped":
                            _rlog.info("poll node=%d app=%d temporarily stopped during restart", node_id, app_id)
                            # Fallback to per-replica stats to avoid losing metrics when
                            # app-level aggregate briefly reports stopped.
                            for replica in replicas:
                                try:
                                    async with AsyncSessionLocal() as rdb:
                                        rcmd = await queue_node_command(
                                            rdb,
                                            node_id=node_id,
                                            app_id=app_id,
                                            command_type="get_replica_stats",
                                            payload={"app_id": app_id, "app_name": app_name, "replica_id": replica.id},
                                            allow_existing_inflight=True,
                                        )
                                        rdone = await wait_for_node_command(rdb, rcmd.id, timeout_seconds=12)
                                    if rdone.status == "done" and rdone.result:
                                        rs = json.loads(rdone.result)
                                        if rs.get("cpu_percent") is not None:
                                            pm.set_replica_stats(replica.id, {"replica_id": replica.id, **rs})
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
                replicas = (await db.execute(select(_AR))).scalars().all()
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
                        cpu, mem, disk = local_cpu, vm.percent, psutil.disk_usage("/").percent
                        mem_total += vm.total / 1024 / 1024
                        mem_used += vm.used / 1024 / 1024
                    else:
                        cpu, mem, disk = n.node_cpu_percent, n.node_memory_percent, n.node_disk_percent
                        try:
                            total = (json.loads(n.metadata_json or "{}") or {}).get("ram_total_mb")
                        except Exception:
                            total = None
                        if total and mem is not None:
                            mem_total += total
                            mem_used += total * mem / 100
                    if cpu is not None:
                        cpu_vals.append(cpu)
                    db.add(NodeMetricsHistory(node_id=n.id, timestamp=now, cpu_percent=cpu,
                                              memory_percent=mem, disk_percent=disk))

                db.add(OverviewHistory(
                    timestamp=now,
                    apps_running=sum(1 for a in apps if a.status == "running"),
                    apps_total=len(apps),
                    instances=sum(1 for r in replicas if r.status == "running"),
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
                    select(Application).where(Application.status == "running")
                )
                apps = result.scalars().all()
                for a in apps:
                    recent = pm.get_recent_stats(a.id)
                    if not recent:
                        continue
                    # Skip per-replica frames (they have replica_id, not cpu_percent at top level)
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
