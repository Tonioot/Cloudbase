"""The autoscaler: every CHECK_INTERVAL it works out how many instances each autoscaled app needs and goes there.

How many: enough that the load per instance stays under the app's targets (CPU %, and optionally memory per
instance), in one step, not one instance at a time (desired_instances). Fast up: the average over the last
`autoscale_up_seconds`. Slow down: the peak over the last `autoscale_down_minutes`, with some headroom, so the
remaining instances are not at their target straight away. Optional per app: at most this many added or removed
per step.

Scale up goes to the least-loaded online nodes (spread over them); scale down removes remote instances first, then
the newest local ones. Scale actions run as background tasks that hold the app (apps.state.acquire_app), so a
deploy and a scale action never overlap. A new instance goes live like any other (apps/rollout.py,
nodes/promote.py); nginx follows the instances by itself. Every action is written to the server log, the app's
live log and the audit log (actor "autoscaler").
"""
import asyncio
import datetime as _dt
import logging
import math
import time as _time
from typing import Optional

import psutil
from fastapi import HTTPException
from sqlalchemy import select

from apps import rollout
from apps.runtime import has_running_instance
from apps.state import acquire_app, busy_with, release_app
from containers import processes as pm
from core import activity, applog
from core.audit import log_audit
from core.database import AsyncSessionLocal
from core.models import Application, Instance, Node, StatsHistory
from nodes.registry import ensure_local_node

alog = logging.getLogger("cloudbase.autoscaler")
ACTOR = "autoscaler"
CHECK_INTERVAL = 15          # seconds between checks
DOWN_HEADROOM = 0.75         # after scaling down, the peak load per instance is at most this part of the target

DEFAULT_UP_SECONDS = 60
DEFAULT_DOWN_MINUTES = 5


# ── The rule ─────────────────────────────────────────────────────────────────

def desired_instances(count: int, cpu_percent: Optional[float], cpu_target: float,
                      memory_mb: Optional[float] = None, memory_target_mb: Optional[float] = None) -> int:
    """Instances needed so that the average CPU per instance stays under cpu_target and (when set) the memory per
    instance under memory_target_mb. cpu_percent is the average per instance now, memory_mb the total of all."""
    need = 0
    if cpu_percent is not None and cpu_target > 0:
        need = math.ceil(round(count * cpu_percent / cpu_target, 6))
    if memory_target_mb and memory_mb is not None:
        need = max(need, math.ceil(round(memory_mb / memory_target_mb, 6)))
    return need


def plan(app, count: int, up: Optional[tuple], down: Optional[tuple]) -> int:
    """How many instances to add (> 0) or remove (< 0) now. `up` is (cpu, memory) averaged over the up window,
    `down` the peaks over the down window; None when that window is not covered yet (or not due)."""
    s = settings(app)
    low, high = s["min"], s["max"]
    if up is not None:
        want = min(high, desired_instances(count, up[0], s["cpu_target"], up[1], s["memory_target_mb"]))
        if want > count:
            return min(want - count, s["max_step_up"] or want)
    if down is not None:
        want = max(low, desired_instances(count, down[0], s["cpu_target"] * DOWN_HEADROOM, down[1],
                                          (s["memory_target_mb"] or 0) * DOWN_HEADROOM or None))
        if want < count:
            return -min(count - want, s["max_step_down"] or count)
    if count < low:
        return low - count                    # e.g. the minimum was raised
    if count > high:
        return -(count - high)                # or the maximum lowered
    return 0


def settings(app) -> dict:
    return {
        "min": app.autoscale_min_instances or 1,
        "max": max(app.autoscale_max_instances or 4, app.autoscale_min_instances or 1),
        "cpu_target": app.autoscale_cpu_target or 70.0,
        "memory_target_mb": app.autoscale_memory_target_mb or None,
        "up_seconds": app.autoscale_up_seconds or DEFAULT_UP_SECONDS,
        "down_seconds": (app.autoscale_down_minutes or DEFAULT_DOWN_MINUTES) * 60,
        "max_step_up": app.autoscale_max_step_up or None,
        "max_step_down": app.autoscale_max_step_down or None,
    }


# ── Measuring ────────────────────────────────────────────────────────────────

def recent_average(app_id: int, seconds: int) -> Optional[tuple[float, float]]:
    """(CPU per instance, total memory MB) averaged over the live samples (in memory, every 5 s) of the last
    `seconds`, or None when they don't cover most of that window yet (e.g. just after a restart)."""
    now_ms = _time.time() * 1000
    frames = [f for f in pm.get_recent_stats(app_id)
              if f.get("cpu_percent") is not None and (f.get("timestamp") or 0) >= now_ms - seconds * 1000]
    if len(frames) < 3 or now_ms - min(f["timestamp"] for f in frames) < seconds * 750:
        return None
    return (sum(f["cpu_percent"] for f in frames) / len(frames),
            sum(f.get("memory_mb") or 0 for f in frames) / len(frames))


async def history_peak(db, app_id: int, seconds: int) -> Optional[tuple[float, float]]:
    """(peak CPU, peak memory) of the 30 s averages over the last `seconds`, when the history covers them."""
    since = _dt.datetime.utcnow() - _dt.timedelta(seconds=seconds)
    rows = (await db.execute(
        select(StatsHistory.cpu_percent, StatsHistory.memory_mb, StatsHistory.timestamp)
        .where(StatsHistory.app_id == app_id, StatsHistory.timestamp >= since)
    )).all()
    if not rows or min(r.timestamp for r in rows) > since + _dt.timedelta(seconds=60):
        return None
    return max(r.cpu_percent for r in rows), max(r.memory_mb for r in rows)


def _describe(load: tuple, s: dict) -> str:
    text = f"CPU {load[0]:.1f}% (target {s['cpu_target']:.0f}%)"
    if s["memory_target_mb"]:
        text += f", memory {load[1]:.0f} MB (target {s['memory_target_mb']:.0f} MB per instance)"
    return text


# ── Acting ───────────────────────────────────────────────────────────────────

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


def _node_load(n: Node) -> float:
    if n.node_cpu_percent is not None:
        return n.node_cpu_percent
    return psutil.cpu_percent(interval=None) if n.is_local else 100.0


def pick_nodes(nodes: list[Node], how_many: int) -> list[Node]:
    """Spread new instances over the online nodes: the fewest new ones first, then the least loaded, then local."""
    added = {n.id: 0 for n in nodes}
    picked = []
    for _ in range(how_many):
        node = min(nodes, key=lambda n: (added[n.id], _node_load(n), not n.is_local))
        added[node.id] += 1
        picked.append(node)
    return picked


def pick_victims(instances: list, local_node_id: int, how_many: int) -> list:
    """Remote instances first (the primary keeps the base load), newest first within each group."""
    ordered = sorted(instances, key=lambda r: (r.node_id == local_node_id, -r.id))
    return ordered[:how_many]


async def _scale_up(app_id: int, count: int, add: int, reason: str) -> None:
    async with AsyncSessionLocal() as s:
        app = await s.get(Application, app_id)
        local = await ensure_local_node(s)
        nodes = list((await s.execute(select(Node).where(Node.enabled == True, Node.status == "online"))).scalars())  # noqa: E712
        if not any(n.id == local.id for n in nodes):
            nodes.append(local)
        new = [(await rollout.add_instance(app, node, s), node.name) for node in pick_nodes(nodes, add)]
    where = ", ".join(f"{i.id} on '{name}'" for i, name in new)
    _say(app_id, f"{reason} — adding {add} instance{'s' if add > 1 else ''} ({where}): {count} → {count + add}…")
    results = await asyncio.gather(*[rollout.wait_until_live(app_id, i.id) for i, _ in new], return_exceptions=True)
    failed = [(i, name, r) for (i, name), r in zip(new, results) if isinstance(r, Exception)]
    if failed:
        await rollout.remove_instances(app_id, [i.id for i, _, _ in failed])
        for i, name, error in failed:
            _say(app_id, f"Instance {i.id} on '{name}' did not come up: {error} — removed.", logging.WARNING)
        await _audit(app_id, "app.autoscale_failed", {"direction": "up", "errors": [str(e) for _, _, e in failed]})
    live = add - len(failed)
    if live:
        _say(app_id, f"Scaled UP to {count + live} instances.")
        await _audit(app_id, "app.autoscale_up", {"added": live, "instances": count + live, "reason": reason})


async def _scale_down(app_id: int, count: int, victims: list[tuple[int, str]], reason: str) -> None:
    where = ", ".join(f"{i} on '{name}'" for i, name in victims)
    _say(app_id, f"{reason} — removing {len(victims)} instance{'s' if len(victims) > 1 else ''} ({where}): "
                 f"{count} → {count - len(victims)}…")
    await rollout.remove_instances(app_id, [i for i, _ in victims])   # out of nginx now; their nodes stop the containers
    _say(app_id, f"Scaled DOWN to {count - len(victims)} instances.")
    await _audit(app_id, "app.autoscale_down", {"removed": len(victims), "instances": count - len(victims), "reason": reason})


# ── The loop ─────────────────────────────────────────────────────────────────

_last_scaled: dict[int, float] = {}       # app id → loop time of the last scale action (or of a deploy we waited for)


async def _run_action(app_id: int, coro) -> None:
    try:
        with activity.track(app_id, "scaling"):
            await coro
    except Exception as e:
        alog.warning("[autoscaler] app_id=%d scale action error: %s", app_id, e)
    finally:
        release_app(app_id)
        _last_scaled[app_id] = asyncio.get_running_loop().time()


async def check_app(db, app, local_node: Node, now: float) -> None:
    """Decide for one app and start the action in the background."""
    s = settings(app)
    since_action = now - _last_scaled[app.id] if app.id in _last_scaled else float("inf")   # (the clock starts at boot)
    if since_action < s["up_seconds"]:
        return
    # A deploy (or our own previous action, or somebody restarting the app) is still running. Rolling and
    # blue-green deploys briefly run old and new instances side by side; scaling down then would leave too few.
    if busy_with(app.id):
        _last_scaled[app.id] = now
        return
    instances = (await db.execute(
        select(Instance).where(Instance.app_id == app.id, Instance.status.in_(["running", "starting"]))
    )).scalars().all()
    if any(r.status == "starting" for r in instances):
        return                                   # still settling
    count = len(instances)
    up = recent_average(app.id, s["up_seconds"])
    # Down only once the whole window lies after the last action: the load was measured with this many instances.
    down = await history_peak(db, app.id, s["down_seconds"]) if since_action >= s["down_seconds"] else None
    change = plan(app, count, up, down)
    if change == 0:
        return

    if change > 0:
        reason = _describe(up, s) if up is not None and count >= s["min"] else f"Below the minimum of {s['min']}"
        action = _scale_up(app.id, count, change, reason)
    else:
        if down is not None and count <= s["max"]:
            reason = f"Peak {_describe(down, s)} over the last {s['down_seconds'] // 60} min"
        else:
            reason = f"Above the maximum of {s['max']}"
        victims = pick_victims(instances, local_node.id, -change)
        names = {n.id: n.name for n in (await db.execute(select(Node).where(Node.id.in_({v.node_id for v in victims})))).scalars()}
        action = _scale_down(app.id, count, [(v.id, names.get(v.node_id, "?")) for v in victims], reason)
    try:
        acquire_app(app.id, "scaling")          # held until the action is done (_run_action)
    except HTTPException:                       # something started during the queries above
        action.close()
        return
    _last_scaled[app.id] = now
    asyncio.create_task(_run_action(app.id, action))


async def _autoscaler():
    await asyncio.sleep(60)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                apps = (await db.execute(
                    select(Application).where(Application.autoscale_enabled == True, has_running_instance())  # noqa: E712
                )).scalars().all()
                local_node = await ensure_local_node(db)
                for app in apps:
                    await check_app(db, app, local_node, asyncio.get_running_loop().time())
        except asyncio.CancelledError:
            return
        except Exception as e:
            alog.warning("[autoscaler] error: %s", e)
        await asyncio.sleep(CHECK_INTERVAL)
