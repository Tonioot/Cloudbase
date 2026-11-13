"""Failover: an app that has lost all its instances because their node went offline gets one on an online node.

Only the stop button sets an instance to "stopped", so that is the one thing that means "do not run". An app is moved
when it has no live instance (running, starting), at least one instance on a node that is offline (for a while, so a
short drop of the connection does not move anything), and none that was stopped on purpose. The replacement is made
like every new instance (apps/rollout.py); once it is live the offline instances are taken off their list for good, so
nothing runs twice when their node comes back (the node removes what is no longer on its list).

Apps that still have a live instance are not touched: the autoscaler adds instances when the load or the minimum asks
for it. A replacement that does not come up is removed and tried again later, on whichever node is online then.
"""
import asyncio
import datetime as _dt
import logging

from fastapi import HTTPException
from sqlalchemy import select

from apps import rollout
from apps.state import acquire_app, busy_with, release_app
from containers import processes as pm
from core import activity, applog
from core.audit import log_audit
from core.database import AsyncSessionLocal
from core.models import Application, Instance, Node
from nodes.registry import ensure_local_node
from workers.autoscaler import pick_nodes

flog = logging.getLogger("cloudbase.failover")
ACTOR = "failover"
CHECK_INTERVAL = 15          # seconds between checks
OFFLINE_GRACE_SECONDS = 60   # a node must have been offline this long before its instances are moved
RETRY_SECONDS = 120          # after a replacement did not come up

_last_try: dict[int, float] = {}       # app id → loop time of the last failover attempt


def needs_failover(statuses: list[str]) -> bool:
    """True for an app that runs nowhere because of an offline node, and was not stopped on purpose."""
    if "node_offline" not in statuses:
        return False
    return not any(s in ("running", "starting", "stopping", "stopped") for s in statuses)


def _say(app_id: int, msg: str, level: int = logging.INFO) -> None:
    flog.log(level, "[failover] app_id=%d %s", app_id, msg, extra={"no_applog": True})
    applog.write(app_id, msg, level, "failover")
    pm._push_container_line(app_id, f"[Failover] {msg}")
    activity.step(app_id, msg)


async def _audit(app_id: int, action: str, detail: dict) -> None:
    try:
        async with AsyncSessionLocal() as s:
            await log_audit(s, action, actor=ACTOR, app_id=app_id, detail=detail)
            await s.commit()
    except Exception as e:
        flog.warning("[failover] app_id=%d audit log failed: %s", app_id, e)


async def _move(app_id: int, offline_ids: list[int], like_id: int) -> None:
    async with AsyncSessionLocal() as s:
        app = await s.get(Application, app_id)
        like = await s.get(Instance, like_id)
        local = await ensure_local_node(s)
        nodes = list((await s.execute(select(Node).where(Node.enabled == True, Node.status == "online"))).scalars())  # noqa: E712
        if not any(n.id == local.id for n in nodes):
            nodes.append(local)
        node = pick_nodes(nodes, 1)[0]
        instance = await rollout.add_instance(app, node, s, like=like)
        name = node.name
    _say(app_id, f"The node of this app is offline — starting instance {instance.id} on '{name}'…")
    try:
        await rollout.wait_until_live(app_id, instance.id)
    except Exception as error:
        await rollout.remove_instances(app_id, [instance.id])
        _say(app_id, f"Instance {instance.id} on '{name}' did not come up: {error} — removed, trying again later.", logging.WARNING)
        await _audit(app_id, "app.failover_failed", {"node": name, "error": str(error)})
        return
    await rollout.remove_instances(app_id, offline_ids)
    _say(app_id, f"Moved to '{name}' (instance {instance.id}).")
    await _audit(app_id, "app.failover", {"node": name, "instance": instance.id, "replaced": offline_ids})


async def _run_action(app_id: int, coro) -> None:
    try:
        with activity.track(app_id, "scaling"):
            await coro
    except Exception as e:
        flog.warning("[failover] app_id=%d error: %s", app_id, e)
    finally:
        release_app(app_id)


async def check_app(db, app: Application, now: float) -> None:
    if app.cron_schedule or busy_with(app.id):
        return
    if app.id in _last_try and now - _last_try[app.id] < RETRY_SECONDS:
        return
    instances = (await db.execute(select(Instance).where(Instance.app_id == app.id))).scalars().all()
    if not needs_failover([i.status for i in instances]):
        return
    offline = [i for i in instances if i.status == "node_offline"]
    nodes = {n.id: n for n in (await db.execute(select(Node).where(Node.id.in_({i.node_id for i in offline})))).scalars()}
    limit = _dt.datetime.utcnow() - _dt.timedelta(seconds=OFFLINE_GRACE_SECONDS)
    if any((n := nodes.get(i.node_id)) is None or n.offline_since is None or n.offline_since > limit for i in offline):
        return                                   # not offline for long enough yet
    try:
        acquire_app(app.id, "scaling")
    except HTTPException:
        return
    _last_try[app.id] = now
    asyncio.create_task(_run_action(app.id, _move(app.id, [i.id for i in offline], offline[0].id)))


async def _failover():
    await asyncio.sleep(60)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                apps = (await db.execute(select(Application).where(Application.id.in_(
                    select(Instance.app_id).where(Instance.status == "node_offline"))))).scalars().all()
                for app in apps:
                    await check_app(db, app, asyncio.get_running_loop().time())
        except asyncio.CancelledError:
            return
        except Exception as e:
            flog.warning("[failover] error: %s", e)
        await asyncio.sleep(CHECK_INTERVAL)
