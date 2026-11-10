"""One-off questions and jobs for a node (logs, stats, Docker cleanup, database work, cron runs).

Instances are not started with commands (that is the desired-state list, nodes/desired.py); what is left is
asking a node something and waiting for the answer. That lives in memory: whoever asks waits for it, and a
question outlives neither the panel nor the agent process that got it, so there is nothing worth storing.

The wire protocol with the agent is unchanged: a command goes out over the websocket as
{"type": "command", "command": {"id", "app_id", "command_type", "payload", ...}} and the answer comes back over
HTTP (POST /api/nodes/agent/commands/{id}/result).
"""
import asyncio
import itertools
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from fastapi import HTTPException, WebSocket
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from nodes.state import _get_node_event, _node_ws_connections, _push_node_event

log = logging.getLogger("cloudbase.nodes")

REUSE_SECONDS = 30            # a question asked again this soon shares the answer of the one already on its way
HISTORY_PER_NODE = 100        # recent commands per node, for the node's page

# Ids keep growing across restarts of the panel, so a late answer to a question from before a restart is
# recognised as unknown instead of being taken for a new one.
_ids = itertools.count(int(time.time() * 1000))


def _utcnow() -> datetime:
    return datetime.utcnow()


@dataclass
class Command:
    node_id: int
    command_type: str
    payload: dict
    app_id: Optional[int] = None
    id: int = field(default_factory=lambda: next(_ids))
    status: str = "queued"                  # queued | in_progress | done | failed
    result: Optional[dict] = None
    error_message: Optional[str] = None
    created_at: datetime = field(default_factory=_utcnow)
    dispatched_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def wire(self) -> dict:
        """What the agent gets."""
        return {"id": self.id, "app_id": self.app_id, "command_type": self.command_type, "payload": self.payload,
                "idempotency_key": str(self.id), "created_at": self.created_at.isoformat()}

    def view(self) -> dict:
        """What the node's page shows."""
        iso = lambda t: t.isoformat() if t else None  # noqa: E731
        return {"id": self.id, "app_id": self.app_id, "command_type": self.command_type, "status": self.status,
                "error_message": self.error_message, "created_at": iso(self.created_at),
                "dispatched_at": iso(self.dispatched_at), "completed_at": iso(self.completed_at)}


_open: dict[int, Command] = {}                     # id -> command not answered yet
_history: dict[int, deque] = {}                    # node id -> its recent commands, newest last


def _remember(cmd: Command) -> None:
    _open[cmd.id] = cmd
    _history.setdefault(cmd.node_id, deque(maxlen=HISTORY_PER_NODE)).append(cmd)


def _finish(cmd: Command, status: str, result: Optional[dict] = None, error: Optional[str] = None) -> None:
    if cmd.status in ("done", "failed"):
        return
    cmd.status, cmd.result, cmd.error_message, cmd.completed_at = status, result, error, _utcnow()
    _open.pop(cmd.id, None)
    cmd.done.set()
    _push_node_event(cmd.node_id, {"type": "command_update", "command_id": cmd.id, "status": status,
                                   "app_id": cmd.app_id, "error_message": error})


def forget(node_id: int) -> None:
    """The node was deleted: its questions will never be answered, and a new node may get its id."""
    for cmd in [c for c in _open.values() if c.node_id == node_id]:
        _finish(cmd, "failed", error="The node was deleted")
    _history.pop(node_id, None)


def history(node_id: int) -> list[dict]:
    """The node's recent commands, newest first."""
    return [c.view() for c in reversed(_history.get(node_id, ()))]


def _reusable(node_id: int, command_type: str, app_id: Optional[int]) -> Optional[Command]:
    cutoff = _utcnow() - timedelta(seconds=REUSE_SECONDS)
    for cmd in _open.values():
        if (cmd.node_id, cmd.command_type, cmd.app_id) == (node_id, command_type, app_id) and cmd.created_at > cutoff:
            return cmd
    return None


async def ask(node_id: int, command_type: str, payload: dict, *, app_id: Optional[int] = None,
              timeout: float = 20.0, reuse: bool = False) -> Command:
    """Send a command to a node and wait for its answer: a Command with status 'done' or 'failed'.

    reuse: share the answer of the same question already on its way (stats, logs, cleanup). Raises
    HTTPException(504) when no answer comes in time; the command is then forgotten."""
    cmd = _reusable(node_id, command_type, app_id) if reuse else None
    if cmd is None:
        cmd = Command(node_id=node_id, command_type=command_type, payload=payload or {}, app_id=app_id)
        _remember(cmd)
        log.info("command %d %s -> node %d (connected=%s)", cmd.id, command_type, node_id, node_id in _node_ws_connections)
        _get_node_event(node_id).set()                 # the node's websocket handler sends it
    try:
        await asyncio.wait_for(cmd.done.wait(), timeout)
    except asyncio.TimeoutError:
        log.warning("command %d %s on node %d: no answer within %.0fs", cmd.id, command_type, node_id, timeout)
        _finish(cmd, "failed", error="Timed out waiting for the node")
        raise HTTPException(504, "Timed out waiting for node command result")
    return cmd


def take_queued(node_id: int) -> list[dict]:
    """The commands for this node that were not sent yet, marked as sent (oldest first)."""
    items = []
    for cmd in sorted(_open.values(), key=lambda c: c.id):
        if cmd.node_id == node_id and cmd.status == "queued":
            cmd.status, cmd.dispatched_at = "in_progress", _utcnow()
            items.append(cmd.wire())
    return items


async def send_queued(node_id: int, websocket: WebSocket) -> int:
    items = take_queued(node_id)
    if items:
        log.info("dispatch %d command(s) to node id=%d: %s", len(items), node_id, [i["command_type"] for i in items])
    for item in items:
        await websocket.send_json({"type": "command", "command": item})
    return len(items)


def answer(node_id: int, command_id: int, status: str, result: Optional[dict] = None, error_message: Optional[str] = None) -> None:
    """A node answered. 404 for a command this panel does not know (it restarted since) or that is another node's."""
    if status not in ("done", "failed"):
        raise HTTPException(400, "Status must be 'done' or 'failed'")
    cmd = _open.get(command_id)
    if cmd is None or cmd.node_id != node_id:
        raise HTTPException(404, "Command not found")
    _finish(cmd, status, result if isinstance(result, dict) else None, error_message)


def fail_in_flight(node_id: int) -> None:
    """The node's agent started over (a new process): what it was working on will never be answered.

    (A cron job is not touched: it is reported after it finishes, and can outlive a restart.)"""
    for cmd in list(_open.values()):
        if cmd.node_id == node_id and cmd.status == "in_progress" and cmd.command_type != "cron_run":
            _finish(cmd, "failed", error="Connection to the node was lost")


async def _commit_with_sqlite_lock_retry(
    db: AsyncSession,
    *,
    context: str,
    attempts: int = 4,
    base_delay_seconds: float = 0.05,
) -> bool:
    """Commit with short retry/backoff for transient SQLite lock contention."""
    for attempt in range(1, attempts + 1):
        try:
            await db.commit()
            return True
        except OperationalError as exc:
            msg = str(exc).lower()
            if "database is locked" not in msg:
                await db.rollback()
                raise
            await db.rollback()
            if attempt >= attempts:
                log.warning("SQLite lock persisted after %d attempts (%s)", attempts, context)
                return False
            await asyncio.sleep(base_delay_seconds * attempt)
