"""What a node reports about its instances, turned into the status the panel shows.

The report says what Docker on the node really does (agent/reconcile.py). This module decides what
that means for the instance: an instance only counts as running (and only gets traffic) once the
primary can also reach it over the node network (nodes/promote.py).
"""
import logging

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import ApplicationReplica
from nodes import promote
from nodes.commands import _utcnow

log = logging.getLogger("cloudbase.nodes")


def _apply_one(replica: ApplicationReplica, report: dict) -> tuple[bool, bool]:
    """Update one instance from its report. Returns (needs a reachability check, nginx must be rewritten)."""
    state = report.get("state")
    if replica.desired != "running":
        if state == "stopped" and replica.status != "stopped":
            replica.status, replica.substatus, replica.container_id = "stopped", None, None
            return False, False
        return False, False

    if report.get("spec_hash") != replica.spec_hash:
        return False, False                         # about an older start of this instance: the newer one is on its way

    was_live = replica.status == "running"
    if state == "running":
        replica.container_id = report.get("container_id") or replica.container_id
        if was_live:
            return False, False
        if replica.status in ("starting", "node_offline", "error", "pending"):
            replica.status, replica.substatus, replica.last_error = "starting", report.get("substatus") or "waiting", None
            return True, False
        return False, False
    if state in ("building", "starting"):
        if replica.status in ("starting", "node_offline", "error", "pending"):
            replica.status, replica.substatus, replica.last_error = "starting", report.get("substatus"), None
        return False, was_live
    if state == "error":
        replica.status, replica.substatus = "error", None
        replica.last_error = (report.get("error") or "the node could not run this instance")[:2000]
        return False, was_live
    # "stopped" or anything else while it should run: the node has not got to it yet
    if replica.status == "node_offline":
        replica.status = "starting"
    return False, was_live


async def apply(node_id: int, reports: dict) -> None:
    """Store a node's report about its instances (only instances placed on that node are touched)."""
    ids = [int(k) for k in reports if str(k).isdigit()]
    if not ids:
        return
    to_check, rewrite = [], set()
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(ApplicationReplica).where(
            ApplicationReplica.node_id == node_id, ApplicationReplica.id.in_(ids)))).scalars().all()
        for replica in rows:
            check, regen = _apply_one(replica, reports.get(str(replica.id)) or {})
            if check:
                to_check.append(replica.id)
            if regen:
                rewrite.add(replica.app_id)
            if db.is_modified(replica):
                replica.updated_at = _utcnow()
        await db.commit()
    for replica_id in to_check:
        promote.start_promotion(replica_id)
    for app_id in rewrite:
        await promote.regen_nginx_for_app(app_id)
