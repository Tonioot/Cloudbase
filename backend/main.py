import asyncio
import json
import logging
import os
import re
import time as _time
from collections import deque
from contextlib import asynccontextmanager
from typing import Optional

import psutil

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, Depends, Body
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware

from database import AsyncSessionLocal, init_db, get_db
from models import Application, User, Role, Permission, role_permissions
from routers import applications, files, logs, stats, nodes, audit as audit_router
from routers import roles as roles_router
from routers import databases as databases_router
from routers import database_backups as database_backups_router
from routers import domains as domains_router
from env_crypto import decrypt_env
from audit import log_audit
import auth
import nginx_manager as nm
import process_manager as pm
import docker_manager as dm
import db_links
import token_vault
import node_agent
import config as _cfg
import system_config as _syscfg

import system_logs

# Must run after the imports above: it replaces any root handlers an imported
# module installed (node_agent used to, which sent everything to node-agent.log).
system_logs.setup_logging()
import applog
applog.install()

log = logging.getLogger("cloudbase.main")

PORT = _cfg.get_server_port()
# A node is managed from its primary's panel and serves no panel of its own.
# Becoming or leaving a node restarts the service, so checking once is enough.
IS_NODE = node_agent.is_node()
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")
_COOKIE_NAME = "pdm_token"
_COOKIE_OPTS = dict(httponly=True, samesite="strict", path="/")

# Restart-loop protection: max 5 restarts per 60s per app
_restart_history: dict[int, list[float]] = {}
MAX_RESTARTS_PER_WINDOW = _cfg.get_limit("max_restarts_per_window")
RESTART_WINDOW_SECONDS = _cfg.get_limit("restart_window_seconds")

_cfg.validate()


async def _node_health_monitor():
    await asyncio.sleep(5)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await nodes.ensure_local_node(db)
                await nodes.mark_stale_nodes_offline(db)
                await nodes.recover_stranded_replicas(db)
        except asyncio.CancelledError:
            return
        except Exception:
            pass
        await asyncio.sleep(5)


def _restore_stuck_restart_configs(apps: list[Application]) -> None:
    for app in apps:
        proxy_port = app.external_port or app.port
        has_custom = bool(app.nginx_enabled and app.domain)
        has_auto = bool(_syscfg.get_base_domain_cached())
        if not ((has_custom or has_auto) and proxy_port):
            continue
        if app.maintenance_mode or app.update_mode:
            continue

        config_path = nm.get_config_path(app.name)
        try:
            if not os.path.exists(config_path):
                continue
            with open(config_path, encoding="utf-8") as f:
                current_config = f.read()
        except Exception:
            continue

        if not nm.config_uses_restart_page(current_config):
            continue

        log.debug(
            f"STARTUP nginx recovery for app {app.id} ({app.name}): "
            "restart page config detected, restoring normal proxy"
        )
        normal_cfg = nm.generate_config(
            app.name,
            app.domain if has_custom else None,
            proxy_port,
            app.ssl_cert_path if has_custom else None,
            app.ssl_key_path if has_custom else None,
            app_id=app.id,
            mode="normal",
        )
        ok, msg = nm.write_nginx_config(app.name, normal_cfg)
        log.debug(f"STARTUP nginx recovery result for app {app.id} ({app.name}): ok={ok} msg={msg!r}")
        if ok:
            pm._push_line(app.id, "Recovered a stale restart page after Cloudbase startup.")


def _docker_runtime_options(app: Application) -> dict:
    return applications._docker_runtime_options(app)


# ── Background stats collector ────────────────────────────────────────────────
async def _stats_collector():
    """Collect process stats for all running apps every 2 s, push to subscribers."""
    await asyncio.sleep(4)
    while True:
        try:
            # Short read — session closed before any Docker/thread work begins.
            from models import Node as _Node, ApplicationReplica as _AppReplica
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
                                dm.get_container_stats_by_name,
                                dm.replica_container_name(a.id, r.id),
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
                    cname = dm.replica_container_name(replica.app_id, replica.id)
                    s = await asyncio.to_thread(dm.get_container_stats_by_name, cname)
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
            from routers.nodes import queue_node_command, wait_for_node_command, _node_ws_connections
            from models import ApplicationReplica as _AR, Application as _App, Node as _Node

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
                import docker_manager as _dm
                import time as _time

                async def _poll_local_replica(r):
                    try:
                        cname = _dm.replica_container_name(r.app_id, r.id)
                        snap = await asyncio.to_thread(_dm.get_container_stats_by_name, cname)
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
            from routers.stats import _aggregate_frames
            for app_id, frames in app_frames.items():
                merged = _aggregate_frames(frames)
                merged.update({"status": "running", "docker": True, "timestamp": int(_time.time() * 1000)})
                pm._stats_history.setdefault(app_id, deque(maxlen=60)).append(merged)

        except asyncio.CancelledError:
            return
        except Exception:
            pass
        await asyncio.sleep(15)


# ── Cluster & node history (dashboard sparklines, node charts) ────────────────
# Sampled in the background and stored in the DB (7 days), so charts are full
# the moment a page opens and survive a Cloudbase restart.
_OVERVIEW_SAMPLE_SECONDS = 30
_HISTORY_RETENTION_DAYS = 7
_HISTORY_MAX_POINTS = 240   # longer ranges are averaged down to this many points


async def _overview_sampler():
    import datetime as _dt
    from sqlalchemy import delete as _delete
    from models import ApplicationReplica as _AR, Node as _Node, OverviewHistory, NodeMetricsHistory
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


def _downsample(rows: list[dict], keys: tuple[str, ...]) -> list[dict]:
    """Average consecutive rows into at most _HISTORY_MAX_POINTS buckets."""
    if len(rows) <= _HISTORY_MAX_POINTS:
        return rows
    size = -(-len(rows) // _HISTORY_MAX_POINTS)   # ceil division
    out = []
    for i in range(0, len(rows), size):
        chunk = rows[i:i + size]
        point = {"ts": chunk[-1]["ts"]}
        for k in keys:
            vals = [r[k] for r in chunk if r.get(k) is not None]
            point[k] = round(sum(vals) / len(vals), 1) if vals else None
        out.append(point)
    return out


def _ts_ms(dt) -> int:
    import datetime as _dt
    return int(dt.replace(tzinfo=_dt.timezone.utc).timestamp() * 1000)


# ── Historical stats writer ───────────────────────────────────────────────────
async def _stats_history_writer():
    """Every 30s write averaged stats from the in-memory deque to the DB for long-term history.

    Works for both local and remote apps — remote apps buffer their stats into pm._stats_history
    via the stats WebSocket relay in routers/stats.py, so we just write whatever has accumulated.
    """
    import datetime as _dt
    from models import StatsHistory
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


# ── Autoscaler ───────────────────────────────────────────────────────────────
async def _autoscaler():
    """Every 15s check CPU for autoscale-enabled apps and add/remove replicas.

    Scale up goes to the least-loaded online node (local or remote); scale down
    removes remote replicas first, then the newest local one. Scale actions run
    as background tasks and mark the app in _scaling_in_progress, so a deploy
    and a scale action never overlap. Nginx is rewritten after every change so
    traffic follows the replica set. Every action is written to the server log,
    the app's live log and the audit log (actor "autoscaler").
    """
    import datetime as _dt
    from models import StatsHistory, ApplicationReplica, Node
    from routers.applications import (
        _assign_external_port, _start_instance_local, _wait_new_instance_healthy,
        _write_app_nginx_config, _has_public_nginx_domain, ensure_local_node,
        _remote_replica_command_payload, _deploys_in_progress, _scaling_in_progress,
        DEPLOY_HEALTH_TIMEOUT_SECONDS,
    )
    from routers.nodes import queue_node_command, wait_for_node_command

    alog = logging.getLogger("cloudbase.autoscaler")
    # Fast up, slow down (no flapping):
    CHECK_INTERVAL = 15  # seconds between checks
    UP_WINDOW = 60       # scale up when the average CPU over this many seconds is above target
    UP_COOLDOWN = 60     # min seconds after any scale action / deploy before scaling up again
    DOWN_WINDOW = 300    # scale down only when CPU stayed below half the target this long;
                         # also the min seconds after any scale action / deploy
    # app_id → time of the last scale action (or of a deploy we waited for)
    _last_scaled: dict[int, float] = {}
    ACTOR = "autoscaler"

    def _recent_cpu_avg(app_id: int, seconds: int) -> Optional[float]:
        """Average of the live CPU samples (in memory; ~2s apart locally, ~15s for
        remote nodes) over the last `seconds`, or None when they don't cover
        most of that window yet (e.g. just after a restart)."""
        now_ms = _time.time() * 1000
        cutoff_ms = now_ms - seconds * 1000
        frames = [
            f for f in pm.get_recent_stats(app_id)
            if f.get("cpu_percent") is not None and (f.get("timestamp") or 0) >= cutoff_ms
        ]
        if len(frames) < 3 or now_ms - min(f["timestamp"] for f in frames) < seconds * 750:
            return None
        return sum(f["cpu_percent"] for f in frames) / len(frames)

    def _say(app_id: int, msg: str, level: int = logging.INFO) -> None:
        # Server log, the app's Cloudbase log (once, with the right level) and its live log.
        alog.log(level, "[autoscaler] app_id=%d %s", app_id, msg, extra={"no_applog": True})
        applog.write(app_id, msg, level, "autoscaler")
        pm._push_container_line(app_id, f"[Autoscaler] {msg}")

    async def _audit(app_id: int, action: str, detail: dict) -> None:
        try:
            async with AsyncSessionLocal() as s:
                await log_audit(s, action, actor=ACTOR, app_id=app_id, detail=detail)
                await s.commit()
        except Exception as e:
            alog.warning("[autoscaler] app_id=%d audit log failed: %s", app_id, e)

    async def _rewrite_nginx(app_id: int) -> None:
        async with AsyncSessionLocal() as s:
            a = await s.get(Application, app_id)
            if a is None or not _has_public_nginx_domain(a):
                return
            local_node = await ensure_local_node(s)
            await _write_app_nginx_config(a, s, local_node)
            await s.commit()

    def _node_load(n: Node) -> float:
        if n.node_cpu_percent is not None:
            return n.node_cpu_percent
        return psutil.cpu_percent(interval=None) if n.is_local else 100.0

    async def _pick_scale_up_node(s, local_node: Node) -> Node:
        res = await s.execute(select(Node).where(Node.enabled == True, Node.status == "online"))
        nodes = list(res.scalars().all())
        if not any(n.id == local_node.id for n in nodes):
            nodes.append(local_node)
        # Least-loaded node wins; on a tie prefer the local node (no tunnel hop).
        return min(nodes, key=lambda n: (_node_load(n), not n.is_local))

    async def _remove_failed_replica(app_id: int, replica_id: int, node: Node) -> None:
        try:
            if node.is_local:
                await asyncio.to_thread(pm.stop_docker_replica, app_id, replica_id)
            else:
                async with AsyncSessionLocal() as s:
                    r = await s.get(ApplicationReplica, replica_id)
                    if r:
                        r.status = "stopping"
                        await s.commit()
                    a = await s.get(Application, app_id)
                    await queue_node_command(
                        s, node_id=node.id, app_id=app_id, command_type="stop_replica",
                        payload={"app_id": app_id, "replica_id": replica_id, "app_name": a.name if a else ""},
                    )
        except Exception as e:
            alog.warning("[autoscaler] app_id=%d cleanup of replica %d failed: %s", app_id, replica_id, e)
        async with AsyncSessionLocal() as s:
            r = await s.get(ApplicationReplica, replica_id)
            if r:
                await s.delete(r)
                await s.commit()
        await _rewrite_nginx(app_id)

    async def _scale_up(app_id: int, count: int, avg_cpu: float, target: float) -> None:
        async with AsyncSessionLocal() as s:
            app = await s.get(Application, app_id)
            local_node = await ensure_local_node(s)
            node = await _pick_scale_up_node(s, local_node)
            env_vars = decrypt_env(app.env_vars or "")
            new_port = await _assign_external_port(None, node.id, None, s)
            replica = ApplicationReplica(
                app_id=app_id,
                node_id=node.id,
                external_port=new_port,
                status="starting",
            )
            s.add(replica)
            await s.commit()
            if not node.is_local:
                await queue_node_command(
                    s, node_id=node.id, app_id=app_id, command_type="start_replica",
                    payload={**_remote_replica_command_payload(app, env_vars, new_port), "replica_id": replica.id},
                )

        _say(app_id, f"CPU {avg_cpu:.1f}% > {target:.0f}% over the last {UP_WINDOW}s — adding instance {replica.id} on '{node.name}' ({count} → {count + 1})…")

        # Docker work and the health check run with no DB session open. A local
        # replica stays "starting" (so not in nginx) until it serves traffic.
        try:
            if node.is_local:
                cid = await _start_instance_local(app, replica, env_vars, app_id)
                check_port = replica.external_port
            else:
                # The agent may need to clone + build first; the tunnel comes up
                # once the container runs (and puts the replica in nginx).
                deadline = asyncio.get_running_loop().time() + DEPLOY_HEALTH_TIMEOUT_SECONDS
                check_port = None
                while asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(3)
                    async with AsyncSessionLocal() as s:
                        fresh = await s.get(ApplicationReplica, replica.id)
                    if fresh is None:
                        raise RuntimeError("instance was removed")
                    if fresh.status == "error":
                        raise RuntimeError(f"node reported an error: {fresh.last_error or 'unknown'}")
                    if fresh.tunnel_port:
                        check_port = fresh.tunnel_port
                        break
                if not check_port:
                    raise RuntimeError(f"tunnel from '{node.name}' did not connect within {DEPLOY_HEALTH_TIMEOUT_SECONDS}s")
            if not app.no_web:
                healthy, reason = await _wait_new_instance_healthy(app_id, replica.id, check_port, is_local=node.is_local)
                if not healthy:
                    raise RuntimeError(f"health check failed: {reason}")
        except Exception as e:
            _say(app_id, f"Scale up failed on '{node.name}': {e} — instance {replica.id} removed.", logging.WARNING)
            await _remove_failed_replica(app_id, replica.id, node)
            await _audit(app_id, "app.autoscale_failed", {"direction": "up", "node": node.name, "error": str(e)})
            return

        if node.is_local:
            async with AsyncSessionLocal() as s:
                r = await s.get(ApplicationReplica, replica.id)
                if r is None:
                    return
                r.status = "running"
                r.substatus = None
                r.container_id = cid
                r.last_error = None
                # _start_instance_local may have reassigned the port on conflict
                r.external_port = replica.external_port
                await s.commit()
        await _rewrite_nginx(app_id)
        _say(app_id, f"Scaled UP to {count + 1} instances (new instance {replica.id} on '{node.name}').")
        await _audit(app_id, "app.autoscale_up", {
            "replica_id": replica.id, "node": node.name, "replicas": count + 1, "cpu": round(avg_cpu, 1),
        })

    async def _scale_down(app_id: int, victim_id: int, count: int, avg_cpu: float, target: float) -> None:
        # Take the victim out of nginx first so no requests hit a dying container.
        async with AsyncSessionLocal() as s:
            victim = await s.get(ApplicationReplica, victim_id)
            if victim is None or victim.status != "running":
                return
            node = await s.get(Node, victim.node_id) if victim.node_id else None
            if node is None:
                node = await ensure_local_node(s)
            victim.status = "stopping"
            await s.commit()
            app_name = (await s.get(Application, app_id)).name
        _say(app_id, f"CPU stayed below {target * 0.5:.0f}% for {DOWN_WINDOW // 60} min (peak {avg_cpu:.1f}%) — removing instance {victim_id} on '{node.name}' ({count} → {count - 1})…")
        await _rewrite_nginx(app_id)

        try:
            if node.is_local:
                stopped = await asyncio.to_thread(pm.stop_docker_replica, app_id, victim_id)
            else:
                async with AsyncSessionLocal() as s:
                    cmd = await queue_node_command(
                        s, node_id=node.id, app_id=app_id, command_type="stop_replica",
                        payload={"app_id": app_id, "replica_id": victim_id, "app_name": app_name},
                    )
                    done = await wait_for_node_command(s, cmd.id, timeout_seconds=120)
                stopped = done.status == "done"
                if not stopped:
                    alog.warning("[autoscaler] app_id=%d stop_replica on '%s' ended %s: %s",
                                app_id, node.name, done.status, done.error_message)
        except Exception as e:
            stopped = False
            alog.warning("[autoscaler] app_id=%d scale down: stop replica %d raised: %s", app_id, victim_id, e)

        async with AsyncSessionLocal() as s:
            victim = await s.get(ApplicationReplica, victim_id)
            # A remote tunnel closing after an intentional stop marks it "stopped".
            if victim and victim.status == "stopped":
                stopped = True
            if stopped:
                if victim:
                    await s.delete(victim)
            elif victim and victim.status == "stopping":
                # Container may still be there — keep the row (no orphan) and put it back in rotation.
                victim.status = "running"
            await s.commit()
        await _rewrite_nginx(app_id)
        if not stopped:
            _say(app_id, f"Scale down aborted: instance {victim_id} on '{node.name}' did not stop, keeping it.", logging.WARNING)
            await _audit(app_id, "app.autoscale_failed", {"direction": "down", "replica_id": victim_id, "node": node.name})
            return
        _say(app_id, f"Scaled DOWN to {count - 1} instances (removed instance {victim_id} on '{node.name}').")
        await _audit(app_id, "app.autoscale_down", {
            "replica_id": victim_id, "node": node.name, "replicas": count - 1, "cpu": round(avg_cpu, 1),
        })

    async def _run_action(app_id: int, coro) -> None:
        try:
            await coro
        except Exception as e:
            alog.warning("[autoscaler] app_id=%d scale action error: %s", app_id, e)
        finally:
            _scaling_in_progress.discard(app_id)
            _last_scaled[app_id] = asyncio.get_running_loop().time()

    await asyncio.sleep(60)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(Application).where(
                        Application.autoscale_enabled == True,
                        Application.status == "running",
                    )
                )
                apps = result.scalars().all()
                local_node = await ensure_local_node(db)

                for app in apps:
                    now = asyncio.get_running_loop().time()
                    since_action = now - _last_scaled.get(app.id, 0)
                    if since_action < UP_COOLDOWN:
                        continue
                    # A deploy (or our own previous scale action) is still running.
                    # Blue-green/rolling deploys briefly run old + new replicas side by
                    # side; scaling down then would kill the new one while the deploy
                    # stops the old one, leaving the app with zero instances.
                    if app.id in _deploys_in_progress or app.id in _scaling_in_progress:
                        _last_scaled[app.id] = now
                        continue

                    # Up: average of the live samples over the last minute — reacts fast.
                    up_avg = _recent_cpu_avg(app.id, UP_WINDOW)

                    # Down: every 30s average of the last 5 minutes must be low, and the
                    # history must actually cover those 5 minutes.
                    down_peak = None
                    if since_action >= DOWN_WINDOW:
                        since = _dt.datetime.utcnow() - _dt.timedelta(seconds=DOWN_WINDOW)
                        hist = await db.execute(
                            select(StatsHistory.cpu_percent, StatsHistory.timestamp)
                            .where(StatsHistory.app_id == app.id, StatsHistory.timestamp >= since)
                        )
                        rows = hist.all()
                        if rows and min(r.timestamp for r in rows) <= since + _dt.timedelta(seconds=60):
                            down_peak = max(r.cpu_percent for r in rows)

                    if up_avg is None and down_peak is None:
                        continue

                    rep_result = await db.execute(
                        select(ApplicationReplica)
                        .where(
                            ApplicationReplica.app_id == app.id,
                            ApplicationReplica.status.in_(["running", "starting"]),
                        )
                        .order_by(ApplicationReplica.id.desc())
                    )
                    replicas = rep_result.scalars().all()
                    count = len(replicas)
                    # Don't act on a replica set that is still settling.
                    if any(r.status == "starting" for r in replicas):
                        continue

                    min_r = app.autoscale_min_replicas or 1
                    max_r = app.autoscale_max_replicas or 4
                    target = app.autoscale_cpu_target or 70.0

                    action = None
                    if up_avg is not None and up_avg > target and count < max_r:
                        action = _scale_up(app.id, count, up_avg, target)
                    elif down_peak is not None and down_peak < target * 0.5 and count > min_r:
                        # Remove remote replicas first (the primary keeps the base load),
                        # newest first within each group (replicas is ordered by id desc).
                        is_local = lambda r: r.node_id is None or r.node_id == local_node.id
                        victim = next((r for r in replicas if not is_local(r)), None) or replicas[0]
                        action = _scale_down(app.id, victim.id, count, down_peak, target)

                    if action is not None:
                        # A deploy may have started during the queries above.
                        if app.id in _deploys_in_progress:
                            action.close()
                            continue
                        # Blocks deploys (see _single_deploy) until the action is done.
                        _scaling_in_progress.add(app.id)
                        _last_scaled[app.id] = now
                        asyncio.create_task(_run_action(app.id, action))

        except asyncio.CancelledError:
            return
        except Exception as e:
            alog.warning("[autoscaler] error: %s", e)
        await asyncio.sleep(CHECK_INTERVAL)


# ── NodeCommand cleanup ───────────────────────────────────────────────────────
async def _node_command_cleanup():
    """Delete completed/failed NodeCommands older than 24h every hour.
    Keeps the table small so scans for 'queued' commands stay fast.
    """
    import datetime as _dt
    from models import NodeCommand as _NC
    from sqlalchemy import delete as _del
    await asyncio.sleep(60)
    while True:
        try:
            cutoff = _dt.datetime.utcnow() - _dt.timedelta(hours=24)
            async with AsyncSessionLocal() as db:
                await db.execute(
                    _del(_NC).where(
                        _NC.status.in_(["done", "failed"]),
                        _NC.completed_at < cutoff,
                    )
                )
                await db.commit()
        except asyncio.CancelledError:
            return
        except Exception:
            pass
        await asyncio.sleep(3600)


# ── Orphan replica cleanup ────────────────────────────────────────────────────
async def _orphan_replica_cleanup():
    """Every 30 min reconcile local replica containers against the DB and remove
    leaks: dead/exited replica containers, and running replica containers that no
    longer have a corresponding ApplicationReplica row. Prevents replicas from
    piling up on the host (which starves memory and destabilises container DNS).
    """
    import docker_manager as _dm
    from models import ApplicationReplica, Node as _Node

    await asyncio.sleep(120)
    while True:
        try:
            containers = await asyncio.to_thread(_dm.list_replica_containers)
            if containers:
                async with AsyncSessionLocal() as db:
                    # Local node id — we only manage local containers here.
                    nr = await db.execute(select(_Node).where(_Node.is_local == True))
                    local_node = nr.scalar_one_or_none()
                    local_node_id = local_node.id if local_node else None

                    # Live replica ids that are expected to be running locally.
                    rep_result = await db.execute(
                        select(ApplicationReplica).where(
                            ApplicationReplica.status.in_(["pending", "starting", "running", "stopping"])
                        )
                    )
                    live = rep_result.scalars().all()
                    live_ids = {
                        r.id for r in live
                        if local_node_id is None or r.node_id is None or r.node_id == local_node_id
                    }

                    for c in containers:
                        rid = c["replica_id"]
                        is_dead = c["status"] in ("exited", "dead", "created")
                        is_orphan = rid not in live_ids
                        if is_dead or is_orphan:
                            removed = await asyncio.to_thread(_dm.remove_container_by_name, c["name"])
                            if removed:
                                reason = "dead" if is_dead else "no DB row"
                                log.info("[orphan-cleanup] removed replica container %s (%s)", c["name"], reason)
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.warning("[orphan-cleanup] error: %s", e)
        await asyncio.sleep(1800)


# ── Crash monitor ─────────────────────────────────────────────────────────────
async def _crash_monitor():
    await asyncio.sleep(5)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                from models import Node as _Node, ApplicationReplica
                local_node_result = await db.execute(select(_Node).where(_Node.is_local == True))
                local_node_obj = local_node_result.scalar_one_or_none()
                local_node_id = local_node_obj.id if local_node_obj else None

                # Monitor local replica containers
                rep_result = await db.execute(
                    select(ApplicationReplica).where(ApplicationReplica.status == "running")
                )
                replicas = rep_result.scalars().all()
                node_map: dict = {}
                for replica in replicas:
                    r_node_id = replica.node_id
                    if r_node_id not in node_map:
                        nr = await db.execute(select(_Node).where(_Node.id == r_node_id))
                        node_map[r_node_id] = nr.scalar_one_or_none()
                    r_node = node_map.get(r_node_id)
                    if r_node and not r_node.is_local:
                        continue  # remote replicas managed by their agent

                    alive = await asyncio.to_thread(dm.is_replica_container_running, replica.app_id, replica.id)
                    if alive:
                        continue

                    app_result = await db.execute(select(Application).where(Application.id == replica.app_id))
                    a = app_result.scalar_one_or_none()
                    if not a:
                        continue

                    policy = a.restart_policy or "no"
                    if policy == "no":
                        replica.status = "stopped"
                        pm._push_line(a.id, f"⚠ Instance {replica.id} exited.")
                        await db.commit()
                        try:
                            from routers.applications import _write_app_nginx_config as _wanc, _has_public_nginx_domain as _hpnd
                            if local_node_obj and _hpnd(a):
                                await _wanc(a, db, local_node_obj)
                        except Exception:
                            pass
                        continue

                    now = _time.time()
                    key = (a.id, replica.id)
                    history = _restart_history.setdefault(key, [])
                    history[:] = [t for t in history if now - t < RESTART_WINDOW_SECONDS]

                    if len(history) >= MAX_RESTARTS_PER_WINDOW:
                        replica.status = "error"
                        replica.last_error = f"Crashed {MAX_RESTARTS_PER_WINDOW}× in {RESTART_WINDOW_SECONDS}s"
                        pm._push_line(a.id, f"✖ Instance {replica.id} crashed {MAX_RESTARTS_PER_WINDOW}× — giving up.")
                        await db.commit()
                        try:
                            from routers.applications import _write_app_nginx_config as _wanc, _has_public_nginx_domain as _hpnd
                            if local_node_obj and _hpnd(a):
                                await _wanc(a, db, local_node_obj)
                        except Exception:
                            pass
                        continue

                    history.append(now)
                    attempt = len(history)
                    pm._push_line(a.id, f"⟳ Instance {replica.id} exited — restarting (attempt {attempt}/{MAX_RESTARTS_PER_WINDOW})…")
                    await asyncio.sleep(min(2 ** attempt, 30))

                    try:
                        env_vars = decrypt_env(a.env_vars or "")
                        cid = await asyncio.to_thread(
                            pm.start_docker_replica,
                            a.id, replica.id, a.name,
                            a.port or 8000, replica.external_port or a.port or 8000,
                            env_vars, _docker_runtime_options(a),
                        )
                        replica.status = "running"
                        replica.container_id = cid
                        replica.last_error = None
                        pm._push_line(a.id, f"✓ Instance {replica.id} restarted ({cid[:12]}).")
                    except Exception as e:
                        replica.status = "error"
                        replica.last_error = str(e)
                        pm._push_line(a.id, f"✖ Instance {replica.id} restart failed: {e}")

                    # Sync app status from all its replicas
                    all_rep_result = await db.execute(
                        select(ApplicationReplica).where(ApplicationReplica.app_id == a.id)
                    )
                    from routers.applications import _derive_app_status_from_instances
                    a.status = _derive_app_status_from_instances(all_rep_result.scalars().all())
                    await db.commit()
                    # If this restart attempt failed, the backend is dead — remove it from nginx upstream
                    if replica.status in ("error", "stopped"):
                        try:
                            from routers.applications import _write_app_nginx_config as _wanc, _has_public_nginx_domain as _hpnd
                            if local_node_obj and _hpnd(a):
                                await _wanc(a, db, local_node_obj)
                        except Exception:
                            pass
        except asyncio.CancelledError:
            return
        except Exception:
            pass
        await asyncio.sleep(5)


# ── Lifespan ──────────────────────────────────────────────────────────────────
def read_panel_nginx() -> dict:
    """Domain and certificate of the panel, read back from its nginx config."""
    import re as _re
    path = os.path.join(nm.NGINX_SITES_DIR, "cloudbase")
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return {}
    def _val(pattern):
        m = _re.search(pattern, content)
        return m.group(1).strip("\"'") if m else None
    return {
        "domain": _val(r"server_name\s+([^\s;]+)"),
        "ssl_cert_path": _val(r"ssl_certificate\s+([^\s;]+)"),
        "ssl_key_path": _val(r"ssl_certificate_key\s+([^\s;]+)"),
    }


def _load_panel_https_domain() -> None:
    """With an HTTPS panel domain the panel is only served through nginx: direct
    requests to this port from other machines go to https://<domain>, so passwords
    and session cookies never cross the network unencrypted."""
    panel = read_panel_nginx()
    https = panel.get("domain") and panel.get("ssl_cert_path") and panel.get("ssl_key_path")
    _syscfg.set_panel_https_domain(panel["domain"] if https else None)


def _refresh_panel_nginx() -> None:
    panel = read_panel_nginx()
    if not panel.get("domain"):
        return
    cert, key = panel.get("ssl_cert_path"), panel.get("ssl_key_path")
    config = nm.generate_config("cloudbase", panel["domain"], PORT, cert, key, strict_hostnames=True)
    ok, msg = nm.write_nginx_config("cloudbase", config)
    if not ok:
        log.warning("STARTUP panel nginx rewrite failed: %s", msg)
        return
    nm.write_default_catch_all(cert, key)


async def _startup_recovery() -> None:
    """Bring local instances, nginx configs and the panel config in line with the DB."""
    # Recover running instances and auto-start
    async with AsyncSessionLocal() as db:
        from models import ApplicationReplica, Node as _Node
        local_node = await nodes.ensure_local_node(db)

        # Recover replica container statuses
        rep_result = await db.execute(select(ApplicationReplica))
        all_replicas = rep_result.scalars().all()
        node_map: dict = {}
        for replica in all_replicas:
            r_node_id = replica.node_id
            if r_node_id not in node_map:
                nr = await db.execute(select(_Node).where(_Node.id == r_node_id))
                node_map[r_node_id] = nr.scalar_one_or_none()
            r_node = node_map.get(r_node_id)
            if r_node and not r_node.is_local:
                continue  # remote replicas managed by their own agent
            if replica.status in ("starting", "stopping", "deploying"):
                continue
            alive = dm.is_replica_container_running(replica.app_id, replica.id)
            new_status = "running" if alive else "stopped"
            if new_status != replica.status:
                replica.status = new_status
                log.debug(f"RECOVERY replica {replica.id} app={replica.app_id}: → {new_status}")
            if alive:
                # Re-attach to the replica's own container (not the legacy cloudbase-app-{id}
                # name), otherwise live logs stay silent after a Cloudbase restart.
                dm.attach_container_log_tailer(
                    replica.app_id, pm.log_buffers, pm._push_container_line, asyncio.get_event_loop(),
                    cname=dm.replica_container_name(replica.app_id, replica.id),
                )

        # Kill any orphan legacy containers (cloudbase-app-{id} without replica row)
        app_ids_with_replicas = {r.app_id for r in all_replicas}
        for app_id in list(app_ids_with_replicas):
            if pm.is_docker_app_running(app_id):
                pm.stop_docker_app(app_id)
                log.debug(f"RECOVERY killed orphan legacy container for app {app_id}")

        # Sync app statuses from their replicas
        result = await db.execute(select(Application))
        apps = result.scalars().all()
        replica_map: dict[int, list] = {}
        for r in all_replicas:
            replica_map.setdefault(r.app_id, []).append(r)

        from routers.applications import _derive_app_status_from_instances
        for a in apps:
            app_replicas = replica_map.get(a.id)
            if app_replicas:
                a.status = _derive_app_status_from_instances(app_replicas)
            # auto_start: start all stopped local replicas
            if a.auto_start and a.start_command and a.working_dir and app_replicas:
                env_vars = decrypt_env(a.env_vars or "")
                for replica in app_replicas:
                    r_node = node_map.get(replica.node_id)
                    if r_node and not r_node.is_local:
                        continue
                    if replica.status != "stopped":
                        continue
                    try:
                        cid = pm.start_docker_replica(
                            a.id, replica.id, a.name,
                            a.port or 8000, replica.external_port or a.port or 8000,
                            env_vars, _docker_runtime_options(a),
                        )
                        replica.status = "running"
                        replica.container_id = cid
                        log.debug(f"AUTO-START replica {replica.id} app={a.id}: {cid[:12]}")
                    except Exception as exc:
                        log.debug(f"AUTO-START replica {replica.id} app={a.id}: FAILED — {exc}")
                a.status = _derive_app_status_from_instances(app_replicas)

        await db.commit()
        await asyncio.to_thread(_restore_stuck_restart_configs, apps)

    # Regenerate nginx configs from live instance state so stale configs on disk
    # (written before the instance-based model) are replaced with correct upstreams.
    async with AsyncSessionLocal() as db:
        from routers.applications import _write_app_nginx_config as _startup_nginx
        from routers.nodes import ensure_local_node as _startup_local_node
        _startup_base = _syscfg.get_base_domain_cached()
        result = await db.execute(select(Application))
        _startup_local = await _startup_local_node(db)
        for a in result.scalars().all():
            has_custom = bool(a.nginx_enabled and a.domain)
            if not has_custom and not _startup_base:
                continue
            try:
                await _startup_nginx(a, db, _startup_local)
                log.debug(f"STARTUP nginx regenerated for app {a.id} ({a.name})")
            except Exception as exc:
                log.debug(f"STARTUP nginx regen failed for app {a.id}: {exc}")

    # The panel's own nginx config is otherwise only written on "Save & apply";
    # rewrite it so it always matches the current generator (e.g. serves the
    # Let's Encrypt verification folder).
    try:
        await asyncio.to_thread(_refresh_panel_nginx)
    except Exception as exc:
        log.warning("STARTUP panel nginx refresh failed: %s", exc)


async def _dev_demo_logs(path: str) -> None:
    """Dev only: feed the demo apps' log views from dev-demo-logs.json (no Docker needed)."""
    import random
    from datetime import datetime
    try:
        with open(path, encoding="utf-8") as f:
            demo = {int(k): v for k, v in json.load(f).items()}
    except (OSError, ValueError) as e:
        log.warning("dev demo logs not loaded from %s: %s", path, e)
        return

    def emit(app_id: int, template: str) -> None:
        line = template.replace("{ts}", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        pm.log_buffers.setdefault(app_id, deque(maxlen=5000)).append(line)
        pm._push_container_line(app_id, line)

    for app_id, d in demo.items():
        for t in d["start"]:
            emit(app_id, t)
        for t in d["loop"] * 3:
            emit(app_id, t)
    while True:
        await asyncio.sleep(1.5)
        app_id, d = random.choice(list(demo.items()))
        emit(app_id, random.choice(d["loop"]))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # First-run: generate a password if none exists. This must happen before
    # init_db(), which seeds the admin user from the credentials file —
    # otherwise the printed password only works after a second start.
    # A node has no panel to log in to.
    if not IS_NODE and not auth.load_hashed_password():
        import secrets
        import string
        alphabet = string.ascii_letters + string.digits
        password = ''.join(secrets.choice(alphabet) for _ in range(16))
        auth.save_hashed_password(auth.hash_password(password))
        print("\n" + "=" * 60)
        print("  Cloudbase — FIRST RUN")
        print(f"  Admin password: {password}")
        print("  Save this — it will not be shown again.")
        print("=" * 60 + "\n")

    await init_db()
    pm.set_main_loop(asyncio.get_event_loop())

    # Keep DB-backed global system settings in memory (with one-time config.yaml bootstrap).
    async with AsyncSessionLocal() as _db:
        await _syscfg.bootstrap_from_config_if_needed(_db)
        await _syscfg.load_cache(_db)

    # Ensure internal agent token exists (used by node_agent.py)
    auth.get_or_create_agent_token()

    # A node is driven by the primary through the agent; the panel loops would
    # only work on its empty local DB, and the orphan cleanup would remove the
    # instances the primary started here.
    tasks: list[asyncio.Task] = []
    if IS_NODE:
        log.info("INIT: running as a node, starting the node agent only")
        tasks.append(asyncio.create_task(node_agent.start_agent()))
    else:
        await _startup_recovery()
        _load_panel_https_domain()
        import deploys as _deploys
        tasks += [
            asyncio.create_task(_crash_monitor()),
            asyncio.create_task(_stats_collector()),
            asyncio.create_task(_node_health_monitor()),
            asyncio.create_task(_stats_history_writer()),
            asyncio.create_task(_remote_replica_stats_poller()),
            asyncio.create_task(_node_command_cleanup()),
            asyncio.create_task(_autoscaler()),
            asyncio.create_task(_orphan_replica_cleanup()),
            asyncio.create_task(_overview_sampler()),
            asyncio.create_task(_deploys.auto_deploy_loop()),
            asyncio.create_task(databases_router.database_monitor()),
            asyncio.create_task(database_backups_router.backup_scheduler()),
            asyncio.create_task(databases_router.database_stats_sampler()),
            asyncio.create_task(databases_router.database_update_checker()),
        ]
        if os.environ.get("CLOUDBASE_DEV_DEMO_LOGS"):
            tasks.append(asyncio.create_task(_dev_demo_logs(os.environ["CLOUDBASE_DEV_DEMO_LOGS"])))

    yield
    for task in tasks:
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=4.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Cloudbase", version="1.0.0", lifespan=lifespan)

# Auth middleware — blocks all /api/ and /ws/ except public paths
_PUBLIC = {
    "/api/health",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/check",
}

# Permission prefix maps — second line of defence (explicit Depends on each endpoint is primary).
# Order matters: more specific prefixes first.
# Write map: fallback permission required for any write to a prefix.
_PERMISSION_WRITE_MAP = (
    ("/api/apps",      "apps.configure"),  # safest fallback; granular checks on endpoints
    ("/api/nodes",     "nodes.configure"),
    ("/api/databases", "databases.manage"),
    ("/api/system",    "system.manage"),
    ("/api/users",     "users.manage"),
    ("/api/roles",     "roles.manage"),
    ("/api/audit-log", "audit.view"),
)

_PERMISSION_READ_MAP = (
    ("/api/apps",      "apps.view"),
    ("/api/nodes",     "nodes.view"),
    ("/api/databases", "databases.view"),
    ("/api/system",    "system.manage"),
    ("/api/users",     "users.manage"),
    ("/api/roles",     "roles.manage"),
    ("/api/audit-log", "audit.view"),
)

# Per-path overrides — used when the prefix default doesn't fit the endpoint's semantics.
_PERMISSION_EXACT_OVERRIDES: dict[tuple[str, str], str] = {
    ("POST", "/api/nodes/invites"): "nodes.add",
    # /api/roles/permissions and /api/roles list are read by any authenticated user (UI needs it)
    ("GET",  "/api/roles"): None,
    ("GET",  "/api/roles/permissions"): None,
}

_PERMISSION_PREFIX_OVERRIDES: tuple[tuple[str, str, str | None], ...] = (
    # (METHOD, path prefix, required permission OR None to allow any authenticated user)
    ("GET",    "/api/roles/",                None),
    ("GET",    "/api/apps/system/certs",     "apps.view"),
    # GitHub token endpoints — under /api/system but use tokens.manage instead
    ("GET",    "/api/system/github-tokens",  "tokens.manage"),
    ("POST",   "/api/system/github-tokens",  "tokens.manage"),
    ("DELETE", "/api/system/github-tokens",  "tokens.manage"),
    # Apps write/delete: each endpoint has its own granular Depends() check.
    # Middleware only requires apps.view as a backstop so it never over-blocks.
    ("POST",   "/api/apps",                  "apps.view"),
    ("PUT",    "/api/apps/",                 "apps.view"),
    ("DELETE", "/api/apps/",                 "apps.view"),
    ("PATCH",  "/api/apps/",                 "apps.view"),
    # Nodes write/delete: same pattern
    ("POST",   "/api/nodes/",                "nodes.view"),
    ("PUT",    "/api/nodes/",                "nodes.view"),
    ("DELETE", "/api/nodes/",                "nodes.view"),
    ("PATCH",  "/api/nodes/",                "nodes.view"),
    # Databases: create / manage / delete are checked per endpoint
    ("POST",   "/api/databases",             "databases.view"),
    ("PATCH",  "/api/databases/",            "databases.view"),
    ("DELETE", "/api/databases/",            "databases.view"),
)

# Sub-path → permission overrides for /api/apps/* endpoints based on URL segments.
# These run AFTER prefix matching and let us require fine-grained read perms
# (logs.view, stats.view) for specific app sub-resources.
def _resolve_apps_get_permission(path: str) -> str:
    """For GET /api/apps/* paths, return the required permission."""
    # /logs, /logs/tail, /replicas/*/logs → apps.view
    if "/logs" in path:
        return "apps.view"
    # /stats, /stats-remote, /aggregate-stats, /stats/history → apps.view
    if "/stats" in path or "stats-remote" in path:
        return "apps.view"
    # Everything else under /api/apps/ → apps.view
    return "apps.view"


def _resolve_required_permission(method: str, path: str) -> tuple[bool, str | None]:
    """
    Return (has_rule, required_permission).
      has_rule=False  → no permission rule applies to this path (allow if authenticated)
      has_rule=True, required_permission=None → explicitly allow any authenticated user
      has_rule=True, required_permission="X"  → require permission X
    """
    # 1. Exact overrides
    if (method, path) in _PERMISSION_EXACT_OVERRIDES:
        return True, _PERMISSION_EXACT_OVERRIDES[(method, path)]

    # 2. Prefix overrides
    for m, prefix, perm in _PERMISSION_PREFIX_OVERRIDES:
        if m == method and path.startswith(prefix) and perm is not None:
            return True, perm

    # 3. Method-based prefix maps
    is_write = method not in ("GET", "HEAD", "OPTIONS")
    table = _PERMISSION_WRITE_MAP if is_write else _PERMISSION_READ_MAP
    for prefix, perm in table:
        if path.startswith(prefix):
            # Special-case GET /api/apps/* — resolve to fine-grained perm
            if not is_write and prefix == "/api/apps":
                return True, _resolve_apps_get_permission(path)
            return True, perm

    return False, None


_NODE_AGENT_PATHS = re.compile(r"/api/apps/\d+/(source-archive|replicas/\d+/substatus)")


def _is_local_client(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def _is_node_traffic(request: Request, path: str) -> bool:
    if path == "/api/health" or path.startswith(("/api/nodes/agent/", "/api/nodes/ws/")):
        return True
    return bool(request.headers.get("X-Node-Token")) and bool(_NODE_AGENT_PATHS.fullmatch(path))


class _AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        method = request.method

        # On a node, the only clients are this server's own agent (X-Agent-Token,
        # also on its local log/stats WebSockets) and health checks.
        if IS_NODE:
            agent_token = request.headers.get("X-Agent-Token", "")
            if path == "/api/health" or (agent_token and auth.verify_agent_token(agent_token)):
                return await call_next(request)
            if path.startswith("/api/") or path.startswith("/ws/"):
                return JSONResponse({"detail": "This server is a Cloudbase node; use the primary's panel"}, status_code=403)
            state = node_agent._load_state()
            page = nm.generate_cloudbase_node_html(state and state.main_url, state and state.node_name)
            return HTMLResponse(page, status_code=403)

        # With an HTTPS panel domain, other machines reach the panel only through
        # nginx (which connects from localhost). Node agents keep using this port.
        # Over an SSH tunnel (ssh -L) the panel still opens here, from localhost.
        panel_domain = _syscfg.get_panel_https_domain()
        if panel_domain and not _is_local_client(request) and not _is_node_traffic(request, path):
            if method in ("GET", "HEAD") and not path.startswith("/api/"):
                query = f"?{request.url.query}" if request.url.query else ""
                return RedirectResponse(f"https://{panel_domain}{path}{query}", status_code=302)
            return JSONResponse({"detail": f"Use the panel at https://{panel_domain}"}, status_code=403)

        # A browser sends Origin on every cross-origin write. Refuse writes from any
        # other site (including apps on a sibling subdomain, which count as
        # same-site and so do get the SameSite=Strict cookie).
        if method not in ("GET", "HEAD", "OPTIONS") and path.startswith("/api/"):
            if not auth.origin_allowed(request.headers.get("origin"), request.headers.get("host")):
                return JSONResponse({"detail": "Cross-origin request refused"}, status_code=403)

        # Node-agent endpoints use X-Node-Token / hello message auth — bypass cookie auth
        if path.startswith("/api/nodes/agent/") or path.startswith("/api/nodes/ws/agent"):
            return await call_next(request)
        if path in _PUBLIC:
            return await call_next(request)

        # WebSocket upgrade requests reach BaseHTTPMiddleware as HTTP GET requests.
        # All /ws/* endpoints authenticate themselves via auth.authorize_websocket(),
        # which now accepts cookies, X-Agent-Token, and X-Node-Token. Pass them through.
        if path.startswith("/ws/"):
            return await call_next(request)

        if path.startswith("/api/") and path not in _PUBLIC:
            token = request.cookies.get(_COOKIE_NAME)
            if token:
                user = await auth.session_user(token)
                if user:
                    # Root (built-in admin) bypasses permission checks entirely
                    if user["username"] == "admin":
                        return await call_next(request)
                    has_rule, required_perm = _resolve_required_permission(method, path)
                    if has_rule and required_perm is not None:
                        perms = await auth.get_user_permissions(user["username"])
                        if required_perm not in perms:
                            return JSONResponse({"detail": "Permission denied"}, status_code=403)
                    return await call_next(request)

            # Allow node_agent.py running locally via X-Agent-Token header
            agent_token = request.headers.get("X-Agent-Token", "")
            if agent_token and auth.verify_agent_token(agent_token):
                return await call_next(request)
            # Remote node agents (X-Node-Token) only reach the few app endpoints
            # they call; those check that the node runs the app.
            node_token = request.headers.get("X-Node-Token", "")
            if node_token and _NODE_AGENT_PATHS.fullmatch(path):
                from sqlalchemy import select as _select, and_ as _and
                from models import Node as _Node
                async with AsyncSessionLocal() as _db:
                    _res = await _db.execute(
                        _select(_Node).where(_and(_Node.auth_token == node_token, _Node.enabled == True))
                    )
                    if _res.scalar_one_or_none():
                        return await call_next(request)
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        return await call_next(request)


app.add_middleware(_AuthMiddleware)
# No CORS middleware: the panel is only used from its own origin, so browsers
# must never let another site read its API responses.
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)


# ── Auth endpoints (public) ───────────────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str
    password: str


@app.get("/api/nodes/{node_id}/metrics/history")
async def node_metrics_history(node_id: int, hours: float = 1,
                               _user: dict = Depends(auth.require_permission("nodes.view")),
                               db: AsyncSession = Depends(get_db)):
    """cpu / memory / disk samples for one node over the last `hours` (oldest first)."""
    import datetime as _dt
    from models import NodeMetricsHistory as _NMH
    hours = max(0.25, min(hours, 24 * _HISTORY_RETENTION_DAYS))
    since = _dt.datetime.utcnow() - _dt.timedelta(hours=hours)
    res = await db.execute(
        select(_NMH).where(_NMH.node_id == node_id, _NMH.timestamp >= since).order_by(_NMH.timestamp.asc())
    )
    rows = [{"ts": _ts_ms(r.timestamp), "cpu": r.cpu_percent, "mem": r.memory_percent, "disk": r.disk_percent}
            for r in res.scalars().all()]
    return {"interval_seconds": _OVERVIEW_SAMPLE_SECONDS, "hours": hours,
            "samples": _downsample(rows, ("cpu", "mem", "disk"))}


@app.get("/api/overview/history")
async def overview_history(hours: float = 1,
                           _user: dict = Depends(auth.require_permission("apps.view")),
                           db: AsyncSession = Depends(get_db)):
    """Cluster samples for the dashboard sparklines over the last `hours` (oldest first)."""
    import datetime as _dt
    from models import OverviewHistory as _OH
    hours = max(0.25, min(hours, 24 * _HISTORY_RETENTION_DAYS))
    since = _dt.datetime.utcnow() - _dt.timedelta(hours=hours)
    res = await db.execute(select(_OH).where(_OH.timestamp >= since).order_by(_OH.timestamp.asc()))
    keys = ("apps_running", "apps_total", "instances", "cpu", "mem_used_mb", "mem_total_mb", "nodes_online", "nodes_total")
    rows = [{
        "ts": _ts_ms(r.timestamp), "apps_running": r.apps_running, "apps_total": r.apps_total,
        "instances": r.instances, "cpu": r.cpu_percent, "mem_used_mb": r.mem_used_mb,
        "mem_total_mb": r.mem_total_mb, "nodes_online": r.nodes_online, "nodes_total": r.nodes_total,
    } for r in res.scalars().all()]
    return {"interval_seconds": _OVERVIEW_SAMPLE_SECONDS, "hours": hours, "samples": _downsample(rows, keys)}


def _read_version() -> str:
    try:
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION")) as f:
            return f.read().strip() or "dev"
    except OSError:
        return "dev"


VERSION = _read_version()


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": VERSION, "port": PORT}


@app.get("/api/auth/check")
async def auth_check(request: Request, db: AsyncSession = Depends(get_db)):
    user = await auth.session_user(request.cookies.get(_COOKIE_NAME))
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    db_user = user["db_user"]
    is_root = db_user.username == "admin"
    perms: list[str] = []
    if is_root:
        perm_res = await db.execute(select(Permission.name))
        perms = [row[0] for row in perm_res.fetchall()]
        return {
            "authenticated": True,
            "username": db_user.username,
            "role": "Root",
            "role_id": None,
            "is_root": True,
            "permissions": perms,
        }
    if db_user.role_id:
        perm_res = await db.execute(
            select(Permission.name)
            .join(role_permissions, Permission.id == role_permissions.c.permission_id)
            .where(role_permissions.c.role_id == db_user.role_id)
        )
        perms = [row[0] for row in perm_res.fetchall()]
    # Fetch role name
    role_name = db_user.role
    if db_user.role_id:
        role_res = await db.execute(select(Role).where(Role.id == db_user.role_id))
        role_obj = role_res.scalar_one_or_none()
        if role_obj:
            role_name = role_obj.name
    return {
        "authenticated": True,
        "username": db_user.username,
        "role": role_name,
        "role_id": db_user.role_id,
        "is_root": False,
        "permissions": perms,
    }


@app.post("/api/auth/login")
async def login(req: LoginRequest, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    client_ip = request.client.host if request.client else "unknown"
    if client_ip in ("127.0.0.1", "::1"):
        # Through the local nginx: limit per visitor, not one shared bucket for everyone
        client_ip = request.headers.get("x-real-ip") or client_ip
    auth._check_rate_limit(client_ip)
    result = await db.execute(select(User).where(User.username == req.username))
    user = result.scalar_one_or_none()
    if not user or not auth.verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = auth.create_access_token(user.username, user.role, user.session_version)
    expire_seconds = auth.get_token_expire_seconds()
    # Secure over HTTPS (nginx sets X-Forwarded-Proto); plain HTTP on an IP keeps working
    https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(key=_COOKIE_NAME, value=token, max_age=expire_seconds, secure=https, **_COOKIE_OPTS)
    await log_audit(db, "auth.login", actor=user.username, detail={"ip": client_ip})
    await db.commit()
    return {"ok": True, "expires_in": expire_seconds, "role": user.role}


@app.get("/api/auth/session")
async def session_info(request: Request):
    token = request.cookies.get(_COOKIE_NAME)
    if not await auth.session_user(token):
        raise HTTPException(status_code=401, detail="Not authenticated")
    expires_in = auth.get_token_expires_in(token)
    if expires_in is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"expires_in": expires_in}


@app.post("/api/auth/logout")
async def logout(response: Response, request: Request):
    response.delete_cookie(key=_COOKIE_NAME, path="/")
    session = await auth.session_user(request.cookies.get(_COOKIE_NAME))
    actor = session["username"] if session else "system"
    async with AsyncSessionLocal() as db:
        if session:
            # Revoke the token itself (it could have been copied), which signs
            # this user out everywhere
            db_user = await db.get(User, session["db_user"].id)
            if db_user:
                db_user.session_version = (db_user.session_version or 0) + 1
        await log_audit(db, "auth.logout", actor=actor)
        await db.commit()
    return {"ok": True}


class ChangePasswordRequest(BaseModel):
    password: str


@app.post("/api/auth/change-password")
async def change_password(req: ChangePasswordRequest, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    user_info = await auth.session_user(request.cookies.get(_COOKIE_NAME))
    if not user_info:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if len(req.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    result = await db.execute(select(User).where(User.username == user_info["username"]))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.password_hash = auth.hash_password(req.password)
    # Sign out every other session; this one gets a fresh token below
    user.session_version = (user.session_version or 0) + 1
    # Keep legacy credentials file in sync for the admin user
    if user.username == "admin":
        auth.save_hashed_password(user.password_hash)
    await log_audit(db, "auth.change_password", actor=user.username, detail={"username": user.username})
    await db.commit()
    expire_seconds = auth.get_token_expire_seconds()
    https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(key=_COOKIE_NAME, value=auth.create_access_token(user.username, user.role, user.session_version),
                        max_age=expire_seconds, secure=https, **_COOKIE_OPTS)
    return {"ok": True}


# ── User management endpoints (superadmin only) ──────────────────────────────
class CreateUserRequest(BaseModel):
    username: str
    password: str
    role_id: int


@app.get("/api/users")
async def list_users(_user: dict = Depends(auth.require_superadmin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).order_by(User.created_at))
    users = result.scalars().all()
    out = []
    for u in users:
        # The Root account has no assignable role — it always has full access
        if u.username == "admin":
            out.append({
                "id": u.id,
                "username": u.username,
                "role": "Root",
                "role_id": None,
                "is_root": True,
                "created_at": u.created_at.isoformat() if u.created_at else None,
            })
            continue
        role_name = u.role
        if u.role_id:
            role_res = await db.execute(select(Role).where(Role.id == u.role_id))
            role_obj = role_res.scalar_one_or_none()
            if role_obj:
                role_name = role_obj.name
        out.append({
            "id": u.id,
            "username": u.username,
            "role": role_name,
            "role_id": u.role_id,
            "is_root": False,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        })
    return out


@app.post("/api/users")
async def create_user(req: CreateUserRequest, admin_user: dict = Depends(auth.require_superadmin), db: AsyncSession = Depends(get_db)):
    if len(req.username.strip()) < 2:
        raise HTTPException(status_code=400, detail="Username must be at least 2 characters")
    if len(req.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    role_res = await db.execute(select(Role).where(Role.id == req.role_id))
    role_obj = role_res.scalar_one_or_none()
    if not role_obj:
        raise HTTPException(status_code=400, detail="Role not found")
    existing = await db.execute(select(User).where(User.username == req.username.strip()))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Username already exists")
    new_user = User(
        username=req.username.strip(),
        password_hash=auth.hash_password(req.password),
        role=role_obj.name,
        role_id=role_obj.id,
    )
    db.add(new_user)
    await log_audit(db, "user.create", actor=admin_user["username"], detail={"username": req.username, "role": role_obj.name})
    await db.commit()
    await db.refresh(new_user)
    return {"id": new_user.id, "username": new_user.username, "role": role_obj.name, "role_id": new_user.role_id}


class UpdateUserRequest(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None
    role_id: Optional[int] = None


@app.put("/api/users/{user_id}")
async def update_user(user_id: int, req: UpdateUserRequest, current_user: dict = Depends(auth.require_superadmin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    detail: dict = {"username": user.username}
    if req.username is not None:
        new_name = req.username.strip()
        if len(new_name) < 2:
            raise HTTPException(status_code=400, detail="Username must be at least 2 characters")
        if user.username == "admin":
            raise HTTPException(status_code=400, detail="Cannot rename the superadmin account")
        existing = await db.execute(select(User).where(User.username == new_name))
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Username already taken")
        detail["old_username"] = user.username
        user.username = new_name
        detail["new_username"] = new_name
    if req.role_id is not None:
        if user.username == "admin":
            raise HTTPException(status_code=400, detail="Cannot change the superadmin role")
        role_res = await db.execute(select(Role).where(Role.id == req.role_id))
        role_obj = role_res.scalar_one_or_none()
        if not role_obj:
            raise HTTPException(status_code=400, detail="Role not found")
        user.role_id = role_obj.id
        user.role = role_obj.name
        detail["role"] = role_obj.name
        detail["role_id"] = role_obj.id
    if req.password is not None:
        if len(req.password) < 8:
            raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
        user.password_hash = auth.hash_password(req.password)
        user.session_version = (user.session_version or 0) + 1  # sign the user out
        if user.username == "admin":
            auth.save_hashed_password(user.password_hash)
        detail["password_changed"] = True
    await log_audit(db, "user.update", actor=current_user["username"], detail=detail)
    await db.commit()
    role_name = user.role
    if user.role_id:
        role_res = await db.execute(select(Role).where(Role.id == user.role_id))
        role_obj = role_res.scalar_one_or_none()
        if role_obj:
            role_name = role_obj.name
    return {"id": user.id, "username": user.username, "role": role_name, "role_id": user.role_id}


@app.delete("/api/users/{user_id}")
async def delete_user(user_id: int, current_user: dict = Depends(auth.require_superadmin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.username == "admin":
        raise HTTPException(status_code=400, detail="Cannot delete the superadmin account")
    await log_audit(db, "user.delete", actor=current_user["username"], detail={"username": user.username})
    await db.delete(user)
    await db.commit()
    return {"ok": True}


# ── System endpoints ──────────────────────────────────────────────────────────
class CloudbaseNginxRequest(BaseModel):
    domain: str
    ssl_cert_path: Optional[str] = None
    ssl_key_path: Optional[str] = None
    base_domain: Optional[str] = None
    base_ssl_cert_path: Optional[str] = None
    base_ssl_key_path: Optional[str] = None


@app.get("/api/system/nginx-config")
async def get_cloudbase_nginx(db: AsyncSession = Depends(get_db), _: dict = Depends(auth.require_permission("system.manage"))):
    config_path = os.path.join(nm.NGINX_SITES_DIR, "cloudbase")
    exists = os.path.exists(config_path)
    content = None
    if exists:
        with open(config_path) as f:
            content = f.read()
    settings = await _syscfg.get_base_settings(db)
    return {
        "exists": exists,
        "content": content,
        "path": config_path,
        "base_domain": settings["base_domain"],
        "base_ssl_cert_path": settings["base_ssl_cert_path"],
        "base_ssl_key_path": settings["base_ssl_key_path"],
    }


@app.post("/api/system/nginx-config")
async def apply_cloudbase_nginx(
    req: CloudbaseNginxRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(auth.require_permission("system.manage")),
):
    import re as _re2
    # --- Cloudbase panel nginx ---
    unavailable_html = nm.generate_cloudbase_unavailable_html(req.domain)
    page_ok, page_msg = nm.write_cloudbase_unavailable_page(unavailable_html)
    if not page_ok:
        raise HTTPException(status_code=500, detail=f"Failed to write Cloudbase unavailable page: {page_msg}")

    unknown_host_html = nm.generate_cloudbase_unknown_host_html(req.domain)
    host_ok, host_msg = nm.write_cloudbase_unknown_host_page(unknown_host_html)
    if not host_ok:
        raise HTTPException(status_code=500, detail=f"Failed to write Cloudbase unknown-host page: {host_msg}")

    config = nm.generate_config(
        "cloudbase",
        req.domain,
        PORT,
        req.ssl_cert_path,
        req.ssl_key_path,
        strict_hostnames=True,
    )
    ok, msg = nm.write_nginx_config("cloudbase", config)
    if not ok:
        raise HTTPException(status_code=500, detail=f"Failed to apply nginx config: {msg}")

    catchall_ok, catchall_msg = nm.write_default_catch_all(req.ssl_cert_path, req.ssl_key_path)
    if not catchall_ok:
        raise HTTPException(status_code=500, detail=f"Cloudbase config applied, but failed to enforce nginx default catch-all: {catchall_msg}")

    # --- Base domain for app subdomains ---
    base_domain = str(req.base_domain or "").strip().strip(".")
    if base_domain and not _re2.match(r'^[a-zA-Z0-9._-]+$', base_domain):
        raise HTTPException(400, "Invalid base_domain")
    base_ssl_cert = str(req.base_ssl_cert_path or "").strip() or None
    base_ssl_key  = str(req.base_ssl_key_path  or "").strip() or None
    await _syscfg.set_base_settings(
        db,
        base_domain=base_domain,
        base_ssl_cert=base_ssl_cert or "",
        base_ssl_key=base_ssl_key or "",
    )

    # Regenerate nginx for all apps in background
    async def _refresh_apps():
        from routers.nodes import ensure_local_node as _eln
        from routers.applications import _write_app_nginx_config as _wanc
        try:
            async with AsyncSessionLocal() as _db:
                _apps = (await _db.execute(select(Application))).scalars().all()
                _local = await _eln(_db)
                _bdom  = _syscfg.get_base_domain_cached()
                for _a in _apps:
                    _has_c = bool(_a.nginx_enabled and _a.domain)
                    if not _has_c and not _bdom:
                        continue
                    try:
                        await _wanc(_a, _db, _local)
                    except Exception as _e:
                        log.warning("nginx refresh failed for app %s: %s", _a.name, _e)
        except Exception as _e:
            log.warning("nginx refresh batch failed: %s", _e)

    asyncio.create_task(_refresh_apps())
    _load_panel_https_domain()
    return {
        "ok": ok,
        "message": msg,
        "preview": config,
        "default_catchall": {"ok": catchall_ok, "message": catchall_msg},
    }


class PanelDomainRequest(BaseModel):
    domain: str
    ssl_cert_path: Optional[str] = None
    ssl_key_path: Optional[str] = None


@app.post("/api/system/panel-domain")
async def set_panel_domain(req: PanelDomainRequest, _: dict = Depends(auth.require_permission("system.manage"))):
    """Set the panel's domain (and certificate) without touching the base domain."""
    domain = nm._normalize_domain(req.domain)
    if not domain:
        raise HTTPException(400, "Enter a domain")
    cert = nm._sanitize_ssl_path(req.ssl_cert_path)
    key = nm._sanitize_ssl_path(req.ssl_key_path)
    for writer, html in (
        (nm.write_cloudbase_unavailable_page, nm.generate_cloudbase_unavailable_html(domain)),
        (nm.write_cloudbase_unknown_host_page, nm.generate_cloudbase_unknown_host_html(domain)),
    ):
        ok, msg = writer(html)
        if not ok:
            raise HTTPException(500, f"Failed to write status page: {msg}")
    config = nm.generate_config("cloudbase", domain, PORT, cert, key, strict_hostnames=True)
    ok, msg = nm.write_nginx_config("cloudbase", config)
    if not ok:
        raise HTTPException(500, f"Failed to apply nginx config: {msg}")
    nm.write_default_catch_all(cert, key)
    _load_panel_https_domain()
    import certificates as _certs
    await _certs.forget_pending([domain])
    return {"ok": True, "domain": domain, "https": bool(cert and key)}


class BaseDomainRequest(BaseModel):
    base_domain: Optional[str] = None


@app.post("/api/system/base-domain")
async def set_base_domain(req: BaseDomainRequest, db: AsyncSession = Depends(get_db), _: dict = Depends(auth.require_permission("system.manage"))):
    """Set (or clear) the base domain for app subdomains. HTTPS is automatic:
    every app gets its own certificate, so no wildcard certificate is kept."""
    import re as _re2
    base_domain = str(req.base_domain or "").strip().strip(".").lower()
    if base_domain and not _re2.match(r'^[a-z0-9.-]+$', base_domain):
        raise HTTPException(400, "Invalid base domain")
    await _syscfg.set_base_settings(db, base_domain=base_domain, base_ssl_cert="", base_ssl_key="")
    asyncio.create_task(_refresh_all_app_nginx())
    return {"ok": True, "base_domain": base_domain}


async def _refresh_all_app_nginx() -> None:
    """Rewrite every app's nginx config (after base domain changes). This also
    schedules automatic certificates for apps on the base domain."""
    from routers.nodes import ensure_local_node as _eln
    from routers.applications import _write_app_nginx_config as _wanc
    try:
        async with AsyncSessionLocal() as _db:
            _apps = (await _db.execute(select(Application))).scalars().all()
            _local = await _eln(_db)
            _bdom = _syscfg.get_base_domain_cached()
            for _a in _apps:
                if not (_a.nginx_enabled and _a.domain) and not _bdom:
                    if not _a.domain:
                        # Was only reachable through the base domain, which is gone
                        await asyncio.to_thread(nm.remove_nginx_config, _a.name)
                    continue
                try:
                    await _wanc(_a, _db, _local)
                except Exception as _e:
                    log.warning("nginx refresh failed for app %s: %s", _a.name, _e)
    except Exception as _e:
        log.warning("nginx refresh batch failed: %s", _e)


# ── System settings (ports, limits from config.yaml) ─────────────────────────
@app.get("/api/system/settings", include_in_schema=False)
async def get_system_settings(_: dict = Depends(auth.require_permission("system.manage"))):
    return {
        "auth": {
            "token_expire_seconds": _cfg.get_auth("token_expire_seconds"),
        },
        "ports": {
            "instance_min": _cfg.get_port("instance_min"),
            "instance_max": _cfg.get_port("instance_max"),
            "tunnel_min":   _cfg.get_port("tunnel_min"),
            "tunnel_max":   _cfg.get_port("tunnel_max"),
        },
        "limits": {
            "max_apps":                _cfg.get_limit("max_apps"),
            "max_instances":           _cfg.get_limit("max_instances"),
            "max_nodes":               _cfg.get_limit("max_nodes"),
            "max_restarts_per_window": _cfg.get_limit("max_restarts_per_window"),
            "restart_window_seconds":  _cfg.get_limit("restart_window_seconds"),
        },
    }


@app.post("/api/system/settings", include_in_schema=False)
async def save_system_settings(
    body: dict = Body(...),
    _: dict = Depends(auth.require_permission("system.manage")),
):
    def _int(val, lo, hi, default):
        try:
            v = int(val)
            if lo <= v <= hi:
                return v
        except (TypeError, ValueError):
            pass
        return default

    auth_cfg = body.get("auth",   {})
    ports    = body.get("ports",  {})
    limits   = body.get("limits", {})
    sections = {}
    if auth_cfg:
        sections["auth"] = {
            "token_expire_seconds": _int(auth_cfg.get("token_expire_seconds"), 60, 2592000, _cfg.get_auth("token_expire_seconds")),
        }
    if ports:
        sections["ports"] = {
            "instance_min": _int(ports.get("instance_min"), 1024, 65000, _cfg.get_port("instance_min")),
            "instance_max": _int(ports.get("instance_max"), 1024, 65000, _cfg.get_port("instance_max")),
            "tunnel_min":   _int(ports.get("tunnel_min"),   1024, 65000, _cfg.get_port("tunnel_min")),
            "tunnel_max":   _int(ports.get("tunnel_max"),   1024, 65000, _cfg.get_port("tunnel_max")),
        }
    if limits:
        sections["limits"] = {
            "max_apps":                _int(limits.get("max_apps"),                1, 100000, _cfg.get_limit("max_apps")),
            "max_instances":           _int(limits.get("max_instances"),           1, 100000, _cfg.get_limit("max_instances")),
            "max_nodes":               _int(limits.get("max_nodes"),               1, 10000,  _cfg.get_limit("max_nodes")),
            "max_restarts_per_window": _int(limits.get("max_restarts_per_window"), 1, 1000,   _cfg.get_limit("max_restarts_per_window")),
            "restart_window_seconds":  _int(limits.get("restart_window_seconds"),  10, 86400, _cfg.get_limit("restart_window_seconds")),
        }
    if sections:
        _cfg.save_config_sections(sections)
    return {"ok": True}


@app.post("/api/system/nginx-default-catchall")
async def apply_nginx_default_catchall(_: dict = Depends(auth.require_permission("system.manage"))):
    ok, msg = nm.write_default_catch_all()
    return {"ok": ok, "message": msg}


@app.post("/api/system/certs/upload")
async def upload_system_cert(file: UploadFile = File(...), _: dict = Depends(auth.require_permission("system.manage"))):
    allowed_exts = {".pem", ".crt", ".cer", ".key"}
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in allowed_exts:
        raise HTTPException(400, "Only .pem, .crt, .cer, .key files are allowed")
    safe_name = os.path.basename(file.filename or "cert").replace("..", "").lstrip("/")
    dest_dir = os.path.expanduser("~/.cloudbase/certs")
    os.makedirs(dest_dir, exist_ok=True)
    if not safe_name or safe_name.startswith("."):
        raise HTTPException(400, "Invalid file name")
    dest_path = os.path.join(dest_dir, safe_name)
    with open(dest_path, "wb") as f:
        f.write(await file.read())
    os.chmod(dest_path, 0o600)  # may be a private key
    return {"path": dest_path}


# ── GitHub token vault ────────────────────────────────────────────────────────
class SaveTokenRequest(BaseModel):
    label: str
    token: str


@app.get("/api/system/github-tokens")
async def list_github_tokens(_: dict = Depends(auth.require_permission("tokens.manage"))):
    return token_vault.list_hints()


@app.post("/api/system/github-tokens")
async def save_github_token(req: SaveTokenRequest, _: dict = Depends(auth.require_permission("tokens.manage"))):
    if not req.label.strip():
        raise HTTPException(400, "Label is required")
    if not req.token.strip():
        raise HTTPException(400, "Token is required")
    token_vault.add(req.label.strip(), req.token.strip())
    return {"ok": True}


@app.delete("/api/system/github-tokens/{token_id}")
async def delete_github_token(token_id: str, _: dict = Depends(auth.require_permission("tokens.manage"))):
    token_vault.remove(token_id)
    return {"ok": True}


# /value endpoint intentionally omitted — raw tokens are resolved server-side only.


# ── System logs ──────────────────────────────────────────────────────────────
@app.get("/api/system/log-sources")
async def get_log_sources(_: dict = Depends(auth.require_permission("system.manage"))):
    return {"sources": system_logs.source_info()}


@app.get("/api/system/logs")
async def get_system_logs(
    source: str = "server",
    limit: int = 500,
    component: Optional[str] = None,
    level: Optional[str] = None,
    q: Optional[str] = None,
    _: dict = Depends(auth.require_permission("system.manage")),
):
    if source not in system_logs.SOURCES:
        raise HTTPException(404, f"Unknown log source '{source}'")
    try:
        return await asyncio.to_thread(system_logs.read_entries, source, limit, component, level, q)
    except PermissionError:
        raise HTTPException(403, f"Cloudbase can't read {system_logs.SOURCES[source][1]} (file permissions)")


from fastapi import WebSocket as _WS, WebSocketDisconnect as _WSD

@app.websocket("/ws/system/logs")
async def stream_system_logs(websocket: _WS, source: str = "server"):
    """Live tail of a log source; sends JSON batches of entries ({entries: [...]})."""
    if not await auth.authorize_websocket(websocket, "system.manage"):
        return
    if source not in system_logs.SOURCES:
        await websocket.close(code=4404)
        return
    await websocket.accept()

    async def _pump():
        try:
            async for batch in system_logs.tail(source):
                if batch:
                    await websocket.send_text(json.dumps({"entries": batch}))
        except (_WSD, asyncio.CancelledError):
            pass
        except Exception as e:
            log.warning("system log stream (%s) ended: %s", source, e)

    # A quiet log never sends, so watch for the disconnect separately —
    # otherwise the tail would keep polling after the tab is closed.
    pump = asyncio.create_task(_pump())
    try:
        while not pump.done():
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break
    except Exception:
        pass
    finally:
        pump.cancel()








# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(applications.router)
app.include_router(nodes.router)
app.include_router(files.router)
app.include_router(logs.router)
app.include_router(stats.router)
app.include_router(audit_router.router)
app.include_router(roles_router.router)
app.include_router(domains_router.router)
app.include_router(databases_router.router)
app.include_router(databases_router.relay_router)
app.include_router(database_backups_router.router)
app.include_router(database_backups_router.agent_router)
app.include_router(database_backups_router.storage_router)


# ── Static / SPA ──────────────────────────────────────────────────────────────
@app.middleware("http")
async def _revalidate_frontend(request: Request, call_next):
    """Make browsers revalidate panel pages and assets (ETag → 304) instead of
    using cached copies after an update. Without it a fresh page could run an
    older cached script, e.g. a button without its click handler."""
    response = await call_next(request)
    path = request.url.path
    if not path.startswith(("/api/", "/ws/")):
        response.headers.setdefault("Cache-Control", "no-cache")
    # The panel may only be framed by itself (visitor page previews), never by
    # another site (clickjacking); responses are never sniffed as another type.
    response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'self'; base-uri 'self'; object-src 'none'")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


if os.path.isdir(FRONTEND_DIR):
    app.mount("/css", StaticFiles(directory=os.path.join(FRONTEND_DIR, "css")), name="css")
    app.mount("/js",  StaticFiles(directory=os.path.join(FRONTEND_DIR, "js")),  name="js")

    @app.get("/favicon.png", include_in_schema=False)
    async def favicon():
        return FileResponse(os.path.join(FRONTEND_DIR, "cloudbase.png"), media_type="image/svg+xml")

    @app.get("/cloudbase.png", include_in_schema=False)
    async def logo():
        return FileResponse(os.path.join(FRONTEND_DIR, "cloudbase.png"), media_type="image/png")

    @app.get("/login", include_in_schema=False)
    @app.get("/login.html", include_in_schema=False)
    async def login_page():
        return FileResponse(os.path.join(FRONTEND_DIR, "login.html"))

    @app.get("/app.html", include_in_schema=False)
    @app.get("/app", include_in_schema=False)
    async def app_page():
        return FileResponse(os.path.join(FRONTEND_DIR, "app.html"))

    @app.get("/node.html", include_in_schema=False)
    @app.get("/node", include_in_schema=False)
    async def node_page():
        return FileResponse(os.path.join(FRONTEND_DIR, "node.html"))

    @app.get("/database.html", include_in_schema=False)
    @app.get("/database", include_in_schema=False)
    async def database_page():
        return FileResponse(os.path.join(FRONTEND_DIR, "database.html"))

    @app.get("/audit.html", include_in_schema=False)
    @app.get("/audit", include_in_schema=False)
    async def audit_page():
        return FileResponse(os.path.join(FRONTEND_DIR, "audit.html"))

    @app.get("/settings.html", include_in_schema=False)
    @app.get("/settings", include_in_schema=False)
    async def settings_page():
        return FileResponse(os.path.join(FRONTEND_DIR, "settings.html"))

    @app.get("/guides.html", include_in_schema=False)
    @app.get("/guides", include_in_schema=False)
    async def guides_page():
        return FileResponse(os.path.join(FRONTEND_DIR, "guides.html"))

    @app.get("/", include_in_schema=False)
    async def index_page():
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))

    @app.get("/{full_path:path}", include_in_schema=False)
    async def catch_all(full_path: str):
        # Never catch API or WebSocket paths
        if full_path.startswith("api/") or full_path.startswith("ws/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        # Paths with a file extension that aren't known static assets → 404
        # (prevents hosting panel paths like /cgi-bin/, /wp-admin/, etc. from redirecting)
        _, ext = os.path.splitext(full_path)
        if ext and ext not in {".html", ".htm"}:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        # Known SPA routes — serve the shell
        _SPA_PREFIXES = ("", "app", "node", "login", "settings", "nodes")
        root = full_path.split("/")[0]
        if root not in _SPA_PREFIXES:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
