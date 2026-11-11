"""The primary runs its own instances the way every node does: from the list (see nodes/desired.py).

This is the same engine the agent on a node runs (containers/reconciler.py) with the HTTP calls replaced by direct
calls: the list comes from the database, the report goes straight into nodes/observed.py. Everything else is shared, so an
instance on the primary starts, is repaired after a crash or a reboot, and is removed in exactly the same way
as one on another node. The only differences are that the source is already here (no download) and that there
is no node network to wait for (nginx reaches the instance on localhost).
"""
import asyncio
import logging

from apps import git as apps_git
from containers import prepare, processes as pm
from containers.reconciler import Reconciler
from core.database import AsyncSessionLocal
from nodes import observed, snapshot
from nodes.registry import ensure_local_node
from nodes.state import _get_node_event

log = logging.getLogger("cloudbase.nodes")


class LocalReconciler(Reconciler):
    def __init__(self, node_id: int):
        super().__init__()
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
        async def fetch(app_id: int) -> str:
            await apps_git.ensure_source(app_id)      # here already, unless this primary was restored or moved
            return pm.get_app_dir(app_id)
        await prepare.ensure_image(spec, progress, fetch, push=pm._push_line)

    async def _watch_nudges(self) -> None:
        """desired.nudge() wakes the node it changed the list of; for the primary that is this reconciler."""
        event = _get_node_event(self.node_id)
        while True:
            await event.wait()
            event.clear()
            self.nudge()

    async def run(self) -> None:
        await asyncio.gather(super().run(), self._watch_nudges())


async def run_forever() -> None:
    """Started with the panel's other background tasks."""
    async with AsyncSessionLocal() as db:
        node_id = (await ensure_local_node(db)).id
    reconciler = LocalReconciler(node_id)
    log.info("[local] reconciling the instances of this server (node %d)", node_id)
    await reconciler.run()
