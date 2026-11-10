"""How the primary reaches an instance: directly, or over the node network.

An instance on another node publishes its port on that node's node-network address only (never on
its public address), so the primary reaches it at <wg_ip>:<external_port> through cbwg0.
"""
import asyncio
import socket

from core.database import AsyncSessionLocal
from core.models import Application, ApplicationReplica, Node

POLL_SECONDS = 2


class Unreachable(RuntimeError):
    pass


def backend_for(replica: ApplicationReplica, node: Node | None) -> str | None:
    """'host:port' that nginx and the health checks use for this instance, or None while it has no address."""
    if not replica.external_port:
        return None
    if node is None or node.is_local:
        return f"127.0.0.1:{replica.external_port}"
    if not node.wg_ip:
        return None
    return f"{node.wg_ip}:{replica.external_port}"


def split(backend: str) -> tuple[str, int]:
    host, _, port = backend.rpartition(":")
    return host, int(port)


def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


async def _publishes_no_port(replica: ApplicationReplica) -> bool:
    """A worker or bot has no web server: there is no port to connect to, it is live once it runs."""
    async with AsyncSessionLocal() as db:
        app = await db.get(Application, replica.app_id)
    return bool(app and app.no_web)


async def wait_reachable(replica_id: int, timeout: float) -> str:
    """Wait until the instance accepts connections; returns its backend.

    Raises Unreachable when the node reports an error, the instance disappears or the time runs out.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        async with AsyncSessionLocal() as db:
            replica = await db.get(ApplicationReplica, replica_id)
            node = await db.get(Node, replica.node_id) if replica else None
        if replica is None:
            raise Unreachable("instance was removed")
        if replica.status == "error":
            raise Unreachable(f"node reported an error: {replica.last_error or 'unknown'}")
        if await _publishes_no_port(replica):
            return ""                       # a worker is live once its node runs it, and promote asks only after that
        backend = backend_for(replica, node)
        if backend and await asyncio.to_thread(port_open, *split(backend)):
            return backend
        if loop.time() >= deadline:
            where = f"'{node.name}'" if node else "the node"
            raise Unreachable(f"{where} did not become reachable over the node network within {int(timeout)}s")
        await asyncio.sleep(POLL_SECONDS)
