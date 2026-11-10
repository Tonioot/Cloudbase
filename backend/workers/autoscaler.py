import asyncio
import logging
import time as _time
from typing import Optional
import psutil
from fastapi import HTTPException
from sqlalchemy import select
from core.database import AsyncSessionLocal
from core import activity
from core.models import Application, Node, StatsHistory, ApplicationReplica
from core.env_crypto import decrypt_env
from core.audit import log_audit
from containers import processes as pm
from core import applog
from apps.validation import _assign_external_port
from apps.runtime import _start_replica_runtime, has_running_instance
from apps.deploy_common import _wait_new_instance_healthy
from mesh import routing
from apps.nginx_sync import _has_public_nginx_domain, _write_app_nginx_config
from nodes.registry import ensure_local_node
from apps.state import DEPLOY_HEALTH_TIMEOUT_SECONDS, acquire_app, busy_with, release_app
from nodes import desired

# ── Autoscaler ───────────────────────────────────────────────────────────────
async def _autoscaler():
    """Every 15s check CPU for autoscale-enabled apps and add/remove replicas.

    Scale up goes to the least-loaded online node (local or remote); scale down
    removes remote replicas first, then the newest local one. Scale actions run
    as background tasks that hold the app (apps.state.acquire_app), so a deploy
    and a scale action never overlap. Nginx is rewritten after every change so
    traffic follows the replica set. Every action is written to the server log,
    the app's live log and the audit log (actor "autoscaler").
    """
    import datetime as _dt

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
        activity.step(app_id, msg)

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
            await _write_app_nginx_config(a, s)
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
        # Least-loaded node wins; on a tie prefer the local node (no network hop).
        return min(nodes, key=lambda n: (_node_load(n), not n.is_local))

    async def _wait_until_stopped(replica_id: int, timeout: float) -> bool:
        """True once the node reports the instance stopped (or it is gone)."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            async with AsyncSessionLocal() as s:
                r = await s.get(ApplicationReplica, replica_id)
            if r is None or r.status == "stopped":
                return True
            await asyncio.sleep(2)
        return False

    async def _remove_failed_replica(app_id: int, replica_id: int, node: Node) -> None:
        desired.nudge(node.id)         # the row is deleted below; the node (this server too) removes the container
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
            new_port = await _assign_external_port(None, node.id, s)
            replica = ApplicationReplica(
                app_id=app_id,
                node_id=node.id,
                external_port=new_port,
                status="starting",
            )
            s.add(replica)
            await s.commit()
            await _start_replica_runtime(app, replica, node, env_vars, s)
            await s.commit()

        _say(app_id, f"CPU {avg_cpu:.1f}% > {target:.0f}% over the last {UP_WINDOW}s — adding instance {replica.id} on '{node.name}' ({count} → {count + 1})…")

        # The health check runs with no DB session open. The node may need to build first; once the container
        # runs the primary can connect to it (and puts it in nginx).
        try:
            try:
                check_backend = await routing.wait_reachable(replica.id, DEPLOY_HEALTH_TIMEOUT_SECONDS)
            except routing.Unreachable as exc:
                raise RuntimeError(str(exc))
            if not app.no_web:
                healthy, reason = await _wait_new_instance_healthy(app_id, replica.id, check_backend)
                if not healthy:
                    raise RuntimeError(f"health check failed: {reason}")
        except Exception as e:
            _say(app_id, f"Scale up failed on '{node.name}': {e} — instance {replica.id} removed.", logging.WARNING)
            await _remove_failed_replica(app_id, replica.id, node)
            await _audit(app_id, "app.autoscale_failed", {"direction": "up", "node": node.name, "error": str(e)})
            return

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
        _say(app_id, f"CPU stayed below {target * 0.5:.0f}% for {DOWN_WINDOW // 60} min (peak {avg_cpu:.1f}%) — removing instance {victim_id} on '{node.name}' ({count} → {count - 1})…")
        await _rewrite_nginx(app_id)

        try:
            async with AsyncSessionLocal() as s:
                await desired.stop(s, node_id=node.id, replica_id=victim_id)
                await s.commit()
            stopped = await _wait_until_stopped(victim_id, timeout=120)
            if not stopped:
                alog.warning("[autoscaler] app_id=%d instance %d on '%s' did not stop in time", app_id, victim_id, node.name)
        except Exception as e:
            stopped = False
            alog.warning("[autoscaler] app_id=%d scale down: stop replica %d raised: %s", app_id, victim_id, e)

        async with AsyncSessionLocal() as s:
            victim = await s.get(ApplicationReplica, victim_id)
            # The node reports "stopped" when an intentional stop completes; an instance deleted by hand is gone too
            if victim is None or victim.status == "stopped":
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
            with activity.track(app_id, "scaling"):
                await coro
        except Exception as e:
            alog.warning("[autoscaler] app_id=%d scale action error: %s", app_id, e)
        finally:
            release_app(app_id)
            _last_scaled[app_id] = asyncio.get_running_loop().time()

    await asyncio.sleep(60)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(Application).where(
                        Application.autoscale_enabled == True,
                        has_running_instance(),
                    )
                )
                apps = result.scalars().all()
                local_node = await ensure_local_node(db)

                for app in apps:
                    now = asyncio.get_running_loop().time()
                    since_action = now - _last_scaled.get(app.id, 0)
                    if since_action < UP_COOLDOWN:
                        continue
                    # A deploy (or our own previous scale action, or somebody restarting the app) is still
                    # running. Blue-green/rolling deploys briefly run old + new replicas side by
                    # side; scaling down then would kill the new one while the deploy
                    # stops the old one, leaving the app with zero instances.
                    if busy_with(app.id):
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
                        # Something may have started during the queries above.
                        try:
                            acquire_app(app.id, "scaling")      # held until the action is done (_run_action)
                        except HTTPException:
                            action.close()
                            continue
                        _last_scaled[app.id] = now
                        asyncio.create_task(_run_action(app.id, action))

        except asyncio.CancelledError:
            return
        except Exception as e:
            alog.warning("[autoscaler] error: %s", e)
        await asyncio.sleep(CHECK_INTERVAL)
