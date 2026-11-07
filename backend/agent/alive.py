"""How a node shows that its agent lives. A node has no web server to ask, so it keeps a small file fresh.

The file holds the process id, the time, and whether the agent is connected to its primary. Scripts treat
it as alive when it is younger than STALE_SECONDS and its process exists (start.sh: node_agent_pid).
"""
import asyncio
import json
import os
import tempfile
import time

from core import paths

INTERVAL_SECONDS = 5
STALE_SECONDS = 30

_connected = False


def set_connected(connected: bool) -> None:
    global _connected
    _connected = connected


def write(now: float | None = None) -> None:
    body = json.dumps({"pid": os.getpid(), "at": int(now if now is not None else time.time()), "connected": _connected})
    directory = os.path.dirname(paths.AGENT_ALIVE_FILE)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".agent.alive.")
    with os.fdopen(fd, "w") as f:
        f.write(body)
    os.replace(tmp, paths.AGENT_ALIVE_FILE)         # never half written


def remove() -> None:
    try:
        os.remove(paths.AGENT_ALIVE_FILE)
    except OSError:
        pass


async def keep_fresh() -> None:
    try:
        while True:
            write()
            await asyncio.sleep(INTERVAL_SECONDS)
    finally:
        remove()
