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


# Public listeners: database id → (server, mode). The mode decides how TLS works:
#   "raw"      no TLS handling, bytes pass through
#   "tls"      TLS from the first byte, terminated here (MongoDB, Redis, Valkey)
#   "postgres" answer the client's SSLRequest, then terminate TLS here
#   "mysql"    MySQL/MariaDB do TLS themselves; refuse clients that don't ask for it
_public_mode: dict[int, tuple] = {}

PG_SSL_REQUEST = 80877103
PG_GSSENC_REQUEST = 80877104
MYSQL_CLIENT_SSL = 0x00000800


def public_mode(engine: str, tls: bool) -> str:
    if not tls:
        return "raw"
    if engine == "postgres":
        return "postgres"
    if engine in ("mysql", "mariadb"):
        return "mysql"
    return "tls"


async def _serve_public(database_id: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = (writer.get_extra_info("peername") or ("?",))[0]
    if not _allowed(database_id, peer):
        log.info("[db-public] database %s: refused %s (not in allowlist)", database_id, peer)
        writer.close()
        return
    mode, ctx = _public_mode.get(database_id, ("raw", None))
    try:
        if mode == "postgres":
            if not await _postgres_tls(reader, writer, ctx):
                return
        elif mode == "mysql":
            await _serve_mysql_tls(database_id, peer, reader, writer)
            return
    except (asyncio.IncompleteReadError, ConnectionError, OSError):
        writer.close()
        return
    await _serve(database_id, reader, writer)


def _pg_error(message: str) -> bytes:
    """A PostgreSQL ErrorResponse, so clients show a readable reason."""
    fields = b"SFATAL\0VFATAL\0C28000\0M" + message.encode() + b"\0\0"
    return b"E" + (len(fields) + 4).to_bytes(4, "big") + fields


async def _postgres_tls(reader, writer, ctx) -> bool:
    """Handle the SSLRequest of a PostgreSQL client and switch to TLS. Returns False
    (connection closed) when the client does not want TLS."""
    while True:
        head = await asyncio.wait_for(reader.readexactly(8), timeout=15)
        length, code = int.from_bytes(head[:4], "big"), int.from_bytes(head[4:], "big")
        if length == 8 and code == PG_GSSENC_REQUEST:
            writer.write(b"N")  # no GSSAPI encryption; the client then asks for TLS
            await writer.drain()
            continue
        if length == 8 and code == PG_SSL_REQUEST:
            writer.write(b"S")
            await writer.drain()
            await writer.start_tls(ctx)
            return True
        writer.write(_pg_error("This database requires TLS. Connect with sslmode=require."))
        await writer.drain()
        writer.close()
        return False


async def _read_packet(read) -> tuple[bytes, bytes]:
    """One MySQL packet: (4-byte header, payload)."""
    header = await read(4)
    return header, await read(int.from_bytes(header[:3], "little"))


async def _serve_mysql_tls(database_id: int, peer: str, reader, writer) -> None:
    """MySQL/MariaDB negotiate TLS inside their own protocol, end to end with the
    server. Forward the server greeting, then require the client's first packet
    to ask for TLS before passing everything through."""
    client = TcpStream(reader, writer)
    if _connector is None:
        await client.close()
        return
    upstream = await _connector(database_id)
    buffer = bytearray()

    async def read_upstream(n: int) -> bytes:
        while len(buffer) < n:
            chunk = await upstream.recv()
            if chunk is None:
                raise ConnectionError("database closed the connection")
            buffer.extend(chunk)
        data = bytes(buffer[:n])
        del buffer[:n]
        return data

    try:
        header, greeting = await asyncio.wait_for(_read_packet(read_upstream), timeout=15)
        server_tls = _mysql_greeting_has_ssl(greeting)
        writer.write(header + greeting + bytes(buffer))
        buffer.clear()
        await writer.drain()

        c_header, c_payload = await asyncio.wait_for(_read_packet(reader.readexactly), timeout=30)
        client_tls = len(c_payload) >= 4 and int.from_bytes(c_payload[:4], "little") & MYSQL_CLIENT_SSL
        if server_tls and not client_tls:
            seq = (c_header[3] + 1) % 256
            body = b"\xff" + (3159).to_bytes(2, "little") + b"#HY000" + \
                b"Connections using insecure transport are prohibited: enable TLS (ssl-mode=REQUIRED)"
            writer.write(len(body).to_bytes(3, "little") + bytes([seq]) + body)
            await writer.drain()
            log.info("[db-public] database %s: refused %s (no TLS)", database_id, peer)
            await client.close()
            await upstream.close()
            return
        await upstream.send(c_header + c_payload)
    except Exception:
        await client.close()
        await upstream.close()
        return
    await pipe(client, upstream)


def _mysql_greeting_has_ssl(greeting: bytes) -> bool:
    """Does the server's handshake (protocol 10) offer CLIENT_SSL?"""
    try:
        end = greeting.index(b"\0", 1)  # server version string
        pos = end + 1 + 4 + 8 + 1          # connection id, auth data part 1, filler
        caps = int.from_bytes(greeting[pos:pos + 2], "little")
        return bool(caps & MYSQL_CLIENT_SSL)
    except (ValueError, IndexError):
        return False


async def reconcile_public(specs: list[dict]) -> None:
    """Run exactly the public listeners in `specs`
    ({database_id, allowlist, engine, tls, hosts})."""
    wanted = {int(s["database_id"]): s for s in specs}
    async with _listener_lock:
        for db_id in list(_public):
            spec = wanted.get(db_id)
            if spec is None or _public_mode.get(db_id, (None,))[0] != public_mode(spec.get("engine", ""), spec.get("tls", False)):
                _public.pop(db_id).close()
                _public_allow.pop(db_id, None)
                _public_mode.pop(db_id, None)
                log.info("[db-public] database %s: public listener closed", db_id)
        for db_id, spec in wanted.items():
            try:
                _public_allow[db_id] = parse_allowlist(spec.get("allowlist") or [])
            except ValueError as e:
                log.warning("[db-public] database %s: invalid allowlist, refusing everyone: %s", db_id, e)
                _public_allow[db_id] = [ipaddress.ip_network("0.0.0.0/32")]
            if db_id in _public:
                continue
            mode = public_mode(spec.get("engine", ""), spec.get("tls", False))
            try:
                ctx = None
                if mode in ("tls", "postgres"):
                    import db_tls
                    ctx = await asyncio.to_thread(db_tls.server_context, tuple(spec.get("hosts") or ()))
                port = public_port(db_id)
                server = await asyncio.start_server(
                    lambda r, w, i=db_id: _serve_public(i, r, w), host=None, port=port,
                    ssl=ctx if mode == "tls" else None,
                    ssl_handshake_timeout=15 if mode == "tls" else None,
                )
                _public[db_id] = server
                _public_mode[db_id] = (mode, ctx)
                log.info("[db-public] database %s: public on port %s (%s)", db_id, port, mode)
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
