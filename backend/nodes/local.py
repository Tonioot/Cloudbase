"""The primary runs its own instances the way every node does: from the list (see nodes/desired.py).

This is the node-agent's reconciler (agent/reconcile.py) with the HTTP calls replaced by direct calls: the list
comes from the database, the report goes straight into nodes/observed.py. Everything else is shared, so an
instance on the primary starts, is repaired after a crash or a reboot, and is removed in exactly the same way
as one on another node. The only differences are that the source is already here (no download) and that there
is no node network to wait for (nginx reaches the instance on localhost).
"""
import asyncio
import logging

from sqlalchemy import text

from agent.reconcile import Reconciler
from agent.source import ensure_image
from containers import processes as pm
from core.database import AsyncSessionLocal
from nodes import observed, snapshot
from nodes.registry import ensure_local_node
from nodes.state import _get_node_event

log = logging.getLogger("cloudbase.nodes")


class LocalReconciler(Reconciler):
    def __init__(self, node_id: int):
        super().__init__(client=None, state=None)
        self.node_id = node_id

    async def _fetch_list(self) -> dict:
        async with AsyncSessionLocal() as db:
            snap = await snapshot.snapshot(db, self.node_id)
        if self.list_hash and snap["hash"] == self.list_hash:
            return {"hash": snap["hash"], "unchanged": True}
        return snap

    async def _send_report(self, body: dict) -> None:
        await observed.apply(self.node_id, body)

    def _network_ready(self) -> bool:
        return True

    def _bind_host(self) -> str | None:
        return None

    async def _prepare(self, spec: dict, progress) -> None:
        await ensure_image(None, None, spec, progress, source=lambda _id, name: pm.get_app_dir(name), push=pm._push_line)

    async def _watch_nudges(self) -> None:
        """desired.nudge() wakes the node it changed the list of; for the primary that is this reconciler."""
        event = _get_node_event(self.node_id)
        while True:
            await event.wait()
            event.clear()
            self.nudge()

    async def run(self) -> None:
        await asyncio.gather(super().run(), self._watch_nudges())


async def adopt_existing(node_id: int) -> None:
    """Instances from before the primary used the list: what they were doing is what they should keep doing.

    They did not know their node (none meant "this server") or whether they should run. The containers they
    already have are adopted, not recreated (they carry no spec label)."""
    async with AsyncSessionLocal() as db:
        await db.execute(text("UPDATE application_replicas SET node_id = :n WHERE node_id IS NULL"), {"n": node_id})
        await db.execute(text(
            "UPDATE application_replicas SET desired = CASE WHEN status IN "
            "('running','starting','restarting','node_offline','pending') THEN 'running' ELSE 'stopped' END "
            "WHERE desired IS NULL AND node_id = :n"), {"n": node_id})
        await db.commit()


async def run_forever() -> None:
    """Started with the panel's other background tasks."""
    async with AsyncSessionLocal() as db:
        node_id = (await ensure_local_node(db)).id
    await adopt_existing(node_id)
    reconciler = LocalReconciler(node_id)
    log.info("[local] reconciling the instances of this server (node %d)", node_id)
    await reconciler.run()
