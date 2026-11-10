import asyncio
import contextlib
import time

from fastapi import HTTPException
from fastapi import WebSocket

# Pulling an image on a slow node can take minutes.
PROVISION_TIMEOUT_SECONDS = 900


COMMAND_TIMEOUT_SECONDS = 120


MONITOR_INTERVAL_SECONDS = 30


HEAL_BACKOFF_SECONDS = 120


DIAL_TIMEOUT_SECONDS = 10


# Databases with an operation in flight; the monitor leaves them alone. One operation at a time per database:
# claim() checks and marks it in one step (no await in between), release() lets go.
_busy: set[int] = set()
# When each database's last operation ended: a node's report from before then no longer says how it is.
_released_at: dict[int, float] = {}


def claim(database_id: int, operation: str | None = None) -> None:
    """Hold the database for one operation, or 409 when another one runs. Pair with release()."""
    if database_id in _busy:
        raise HTTPException(409, "Another operation on this database is still running")
    _busy.add(database_id)
    if operation:
        _operation[database_id] = operation


@contextlib.contextmanager
def held(database_id: int, operation: str | None = None):
    claim(database_id, operation)
    try:
        yield
    finally:
        release(database_id)


def release(database_id: int) -> None:
    """An operation on the database is over."""
    _busy.discard(database_id)
    _operation.pop(database_id, None)
    _released_at[database_id] = time.monotonic()


_operation: dict[int, str] = {}  # what a busy database is doing, e.g. "Backing up…"


# Result of the last update check per database: {version, update_available, latest_version, error, checked_at}
_update_info: dict[int, dict] = {}


_last_heal: dict[int, float] = {}


# Cross-node connections: control channel per node, dials waiting for their node.
_control: dict[int, tuple[WebSocket, asyncio.Lock]] = {}


_pending_dials: dict[str, tuple[asyncio.Future, int]] = {}


_remote_progress: dict[int, dict[str, str]] = {}  # node id -> {database id: progress}
