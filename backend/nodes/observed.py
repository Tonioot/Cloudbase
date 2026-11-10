"""What a node reports about its instances, turned into the status the panel shows.

The report says what Docker on the node really does (containers/reconciler.py). This module decides what
that means for the instance: an instance only counts as running (and only gets traffic) once the
primary can also reach it over the node network (nodes/promote.py).
"""
import logging

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import Instance
from apps import nginx_sync
from containers import processes as pm
from nodes import promote
from nodes.commands import _utcnow

log = logging.getLogger("cloudbase.nodes")


def _apply_one(instance: Instance, report: dict) -> tuple[bool, bool]:
    """Update one instance from its report. Returns (needs a reachability check, nginx must be rewritten)."""
    state = report.get("state")
    if instance.desired != "running":
        if state == "stopped" and instance.status != "stopped":
            instance.status, instance.substatus, instance.container_id = "stopped", None, None
            return False, False
        return False, False

    if report.get("spec_hash") != instance.spec_hash:
        return False, False                         # about an older start of this instance: the newer one is on its way

    was_live = instance.status == "running"
    if state == "running":
        instance.container_id = report.get("container_id") or instance.container_id
        if was_live:
            return False, False
        if instance.status in ("starting", "node_offline", "error", "pending"):
            instance.status, instance.substatus, instance.last_error = "starting", report.get("substatus") or "waiting", None
            return True, False
        return False, False
    if state in ("building", "starting"):
        if instance.status in ("starting", "node_offline", "error", "pending"):
            instance.status, instance.substatus, instance.last_error = "starting", report.get("substatus"), None
        return False, was_live
    if state == "error":
        instance.status, instance.substatus = "error", None
        instance.last_error = (report.get("error") or "the node could not run this instance")[:2000]
        return False, was_live
    # "stopped" or anything else while it should run: the node has not got to it yet
    if instance.status == "node_offline":
        instance.status = "starting"
    return False, was_live


async def apply(node_id: int, reports: dict) -> None:
    """Store a node's report about its instances (only instances placed on that node are touched)."""
    ids = [int(k) for k in reports if str(k).isdigit()]
    if not ids:
        return
    to_check, rewrite, lines = [], set(), []
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(Instance).where(
            Instance.node_id == node_id, Instance.id.in_(ids)))).scalars().all()
        for instance in rows:
            before, step = instance.status, instance.substatus
            check, regen = _apply_one(instance, reports.get(str(instance.id)) or {})
            if (instance.status, instance.substatus) != (before, step):
                lines.append((instance.app_id, instance.id, instance.status, instance.substatus, instance.last_error))
            if check:
                to_check.append(instance.id)
            if regen or instance.status != before:          # nginx shows what the instances do (apps/nginx_sync.py)
                rewrite.add(instance.app_id)
            if db.is_modified(instance):
                instance.updated_at = _utcnow()
        await db.commit()
    for line in lines:
        pm.instance_line(*line)
    for instance_id in to_check:
        promote.start_promotion(instance_id)
    for app_id in rewrite:
        await nginx_sync.refresh_app_nginx(app_id)
