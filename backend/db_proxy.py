"""Database connections across nodes. Runs on the primary and on every node.

An app instance on the database's own node connects straight to the database
container over the `cloudbase` Docker network. An instance on any other node
connects to a small TCP proxy on its own node instead:

    app container ─► <gateway of the cloudbase network>:<proxy port>   (this module)
        ─► upstream connector:
             on a node:    one binary WebSocket to the primary per connection
             on primary:   the database container directly, or a dial to its node

The primary decides which proxies each node runs and pushes that list over the
agent's database control channel; this module only opens and closes listeners.
Every connection gets its own WebSocket, so there is no multiplexing protocol and
the bytes travel as binary frames.
"""

import asyncio
import ipaddress
import logging
from typing import Awaitable, Callable, Optional

import config as _cfg

log = logging.getLogger("cloudbase.db-proxy")

CHUNK = 65536

_loop: Optional[asyncio.AbstractEventLoop] = None
_connector: Optional[Callable[[int], Awaitable["Stream"]]] = None
_local_node_id: Optional[int] = None
_listeners: dict[int, asyncio.base_events.Server] = {}
_listener_lock = asyncio.Lock()
# Public access (primary only): database id → server, and its allowed networks.
_public: dict[int, asyncio.base_events.Server] = {}
_public_allow: dict[int, list] = {}
_gateway_ip: Optional[str] = None


# ── Byte streams ──────────────────────────────────────────────────────────────

class Stream:
    async def recv(self) -> Optional[bytes]:
        raise NotImplementedError

    async def send(self, data: bytes) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


class TcpStream(Stream):
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader, self.writer = reader, writer

    async def recv(self) -> Optional[bytes]:
        data = await self.reader.read(CHUNK)
        return data or None

    async def send(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()

    async def close(self) -> None:
        try:
            self.writer.close()
        except Exception:
            pass


class ClientWebSocketStream(Stream):
    """A `websockets` client connection (used by node agents)."""

    def __init__(self, ws):
        self.ws = ws

    async def recv(self) -> Optional[bytes]:
        try:
            data = await self.ws.recv()
        except Exception:
            return None
        return data if isinstance(data, bytes) else data.encode()

    async def send(self, data: bytes) -> None:
        await self.ws.send(data)

    async def close(self) -> None:
        try:
            await self.ws.close()
        except Exception:
            pass


class ServerWebSocketStream(Stream):
    """A Starlette WebSocket (used by the primary). `closed` is set when it ends,
    so the endpoint handler can return at the right moment."""

    def __init__(self, ws):
        self.ws = ws
        self.closed = asyncio.Event()

    async def recv(self) -> Optional[bytes]:
        try:
            msg = await self.ws.receive()
        except Exception:
            return None
        if msg.get("type") == "websocket.disconnect":
            return None
        return msg.get("bytes") or (msg.get("text") or "").encode() or b""

    async def send(self, data: bytes) -> None:
        await self.ws.send_bytes(data)

    async def close(self) -> None:
        if not self.closed.is_set():
            try:
                await self.ws.close()
            except Exception:
                pass
        self.closed.set()


async def pipe(a: Stream, b: Stream) -> None:
    """Copy bytes both ways until either side ends, then close both."""
    async def copy(src: Stream, dst: Stream) -> None:
        try:
            while True:
                data = await src.recv()
                if data is None:
                    break
                if data:
                    await dst.send(data)
        except Exception:
            pass

    tasks = [asyncio.create_task(copy(a, b)), asyncio.create_task(copy(b, a))]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await a.close()
        await b.close()


# ── Configuration ─────────────────────────────────────────────────────────────

def configure(loop: asyncio.AbstractEventLoop, connector: Callable[[int], Awaitable[Stream]],
              local_node_id: Optional[int] = None) -> None:
    """Called once per process: by the node agent, or by the primary's monitor."""
    global _loop, _connector
    _loop, _connector = loop, connector
    if local_node_id is not None:
        set_local_node_id(local_node_id)


def set_local_node_id(node_id: Optional[int]) -> None:
    global _local_node_id
    _local_node_id = node_id


def local_node_id() -> Optional[int]:
    if _local_node_id is not None:
        return _local_node_id
    try:
        import node_agent
        state = node_agent._load_state()
        return state.node_id if state else None
    except Exception:
        return None


def proxy_port(database_id: int) -> int:
    port = _cfg.get_port("db_proxy_min") + int(database_id)
    if port > _cfg.get_port("db_proxy_max"):
        raise RuntimeError(f"Database id {database_id} is outside the db_proxy port range in config.yaml")
    return port


def gateway_ip() -> str:
    """Address of this host on the cloudbase network: reachable by containers, not from outside."""
    global _gateway_ip
    if _gateway_ip:
        return _gateway_ip
    import db_manager
    import docker_manager as dm
    dm.ensure_network(db_manager.NETWORK)
    net = dm._get_client().networks.get(db_manager.NETWORK)
    _gateway_ip = net.attrs["IPAM"]["Config"][0]["Gateway"]
    return _gateway_ip


# ── Listeners ─────────────────────────────────────────────────────────────────

async def _serve(database_id: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    client = TcpStream(reader, writer)
    if _connector is None:
        await client.close()
        return
    try:
        upstream = await _connector(database_id)
    except Exception as e:
        log.warning("[db-proxy] database %s: upstream connection failed: %s", database_id, e)
        await client.close()
        return
    await pipe(client, upstream)


async def _open_listener(database_id: int) -> None:
    if database_id in _listeners:
        return
    host = await asyncio.to_thread(gateway_ip)
    port = proxy_port(database_id)
    server = await asyncio.start_server(lambda r, w: _serve(database_id, r, w), host, port)
    _listeners[database_id] = server
    log.info("[db-proxy] database %s: listening on %s:%s", database_id, host, port)


async def reconcile(database_ids: list[int]) -> None:
    """Run exactly the proxies in `database_ids` on this server."""
    wanted = {int(i) for i in database_ids}
    async with _listener_lock:
        for db_id in list(_listeners):
            if db_id not in wanted:
                server = _listeners.pop(db_id)
                server.close()
                log.info("[db-proxy] database %s: listener closed", db_id)
        for db_id in wanted:
            try:
                await _open_listener(db_id)
            except Exception as e:
                log.warning("[db-proxy] database %s: cannot listen: %s", db_id, e)


async def _ensure(database_id: int) -> None:
    async with _listener_lock:
        await _open_listener(database_id)


def _container_here(database_id: int) -> bool:
    import db_manager
    import docker_manager as dm
    try:
        return db_manager._get_container(dm._get_client(), database_id) is not None
    except Exception:
        return False


# ── Public access (primary) ───────────────────────────────────────────────────

def public_port(database_id: int) -> int:
    port = _cfg.get_port("db_public_min") + int(database_id)
    if port > _cfg.get_port("db_public_max"):
        raise RuntimeError(f"Database id {database_id} is outside the db_public port range in config.yaml")
    return port


def parse_allowlist(entries: list[str]) -> list:
    """IPs and CIDRs → networks. Raises ValueError on an invalid entry."""
    return [ipaddress.ip_network(e.strip(), strict=False) for e in entries if e.strip()]


def _allowed(database_id: int, peer_ip: str) -> bool:
    networks = _public_allow.get(database_id) or []
    if not networks:
        return True
    try:
        ip = ipaddress.ip_address(peer_ip)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in networks)


async def _serve_public(database_id: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = (writer.get_extra_info("peername") or ("?",))[0]
    if not _allowed(database_id, peer):
        log.info("[db-public] database %s: refused %s (not in allowlist)", database_id, peer)
        writer.close()
        return
    await _serve(database_id, reader, writer)


async def reconcile_public(specs: list[dict]) -> None:
    """Run exactly the public listeners in `specs` ({database_id, allowlist})."""
    wanted = {int(s["database_id"]): s.get("allowlist") or [] for s in specs}
    async with _listener_lock:
        for db_id in list(_public):
            if db_id not in wanted:
                _public.pop(db_id).close()
                _public_allow.pop(db_id, None)
                log.info("[db-public] database %s: public access closed", db_id)
        for db_id, allowlist in wanted.items():
            try:
                _public_allow[db_id] = parse_allowlist(allowlist)
            except ValueError as e:
                log.warning("[db-public] database %s: invalid allowlist, refusing everyone: %s", db_id, e)
                _public_allow[db_id] = [ipaddress.ip_network("0.0.0.0/32")]
            if db_id in _public:
                continue
            try:
                port = public_port(db_id)
                server = await asyncio.start_server(
                    lambda r, w, i=db_id: _serve_public(i, r, w), host=None, port=port,
                )
                _public[db_id] = server
                log.info("[db-public] database %s: public on port %s", db_id, port)
            except Exception as e:
                log.warning("[db-public] database %s: cannot open public port: %s", db_id, e)


def public_listening(database_id: int) -> bool:
    return database_id in _public


def resolve_links(linked: list[dict]) -> tuple[dict, dict]:
    """For a container about to start here: env vars and /etc/hosts entries for its
    linked databases. Called from a worker thread (docker_manager)."""
    me = local_node_id()
    env: dict[str, str] = {}
    hosts: dict[str, str] = {}
    for link in linked:
        # Node id unknown (e.g. right after the primary starts): the database is
        # local exactly when its container exists on this host.
        local = link["node_id"] == me if me is not None else _container_here(int(link["database_id"]))
        if local:
            env[link["env_var"]] = link["direct_url"]
            continue
        env[link["env_var"]] = link["proxy_url"]
        hosts[link["host"]] = gateway_ip()
        # Make sure the proxy is up before the app tries to connect; the primary's
        # periodic push would otherwise add it within half a minute.
        if _loop is not None and _loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(_ensure(int(link["database_id"])), _loop).result(timeout=10)
            except Exception as e:
                log.warning("[db-proxy] database %s: listener not ready: %s", link["database_id"], e)
    return env, hosts
