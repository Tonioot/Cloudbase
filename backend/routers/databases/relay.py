import asyncio
import json
import logging
from fastapi import WebSocket, APIRouter
from dbs import proxy as db_proxy
from dbs.relay import _authorize_node, _desired_proxy_ids, open_db_stream, push_proxies
from dbs.state import _control, _pending_dials, _remote_progress

# Node-facing WebSockets for database connections across nodes.
relay_router = APIRouter(prefix="/api/nodes/ws", tags=["databases"])


log = logging.getLogger("cloudbase.databases")


@relay_router.websocket("/db-control")
async def db_control_ws(websocket: WebSocket):
    """Per-node control channel: proxy lists and dial requests go out here."""
    node = await _authorize_node(websocket)
    if node is None:
        return
    await websocket.accept()
    old = _control.get(node.id)
    _control[node.id] = (websocket, asyncio.Lock())
    if old is not None:
        try:
            await old[0].close(code=1012)
        except Exception:
            pass
    log.info("[db-relay] control channel of node %s connected", node.name)
    try:
        await push_proxies()  # fresh list, e.g. after the node restarted
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            try:
                data = json.loads(msg.get("text") or "{}")
            except ValueError:
                continue
            if data.get("type") == "progress":
                _remote_progress[node.id] = {str(k): str(v) for k, v in (data.get("progress") or {}).items()}
    except Exception:
        pass
    finally:
        current = _control.get(node.id)
        if current is not None and current[0] is websocket:
            _control.pop(node.id, None)
            _remote_progress.pop(node.id, None)
        log.info("[db-relay] control channel of node %s disconnected", node.name)


@relay_router.websocket("/db-dial/{token}")
async def db_dial_ws(token: str, websocket: WebSocket):
    """A database's node answering a dial: this socket carries one connection."""
    node = await _authorize_node(websocket)
    if node is None:
        return
    pending = _pending_dials.get(token)
    if pending is None or pending[1] != node.id or pending[0].done():
        await websocket.close(code=1008, reason="Unknown or expired dial")
        return
    await websocket.accept()
    stream = db_proxy.ServerWebSocketStream(websocket)
    pending[0].set_result(stream)
    await stream.closed.wait()


@relay_router.websocket("/db-proxy/{database_id}")
async def db_proxy_ws(database_id: int, websocket: WebSocket):
    """One database connection from an app instance on a node."""
    node = await _authorize_node(websocket)
    if node is None:
        return
    if database_id not in _desired_proxy_ids(node.id):
        await push_proxies()  # an instance may have started since the last push
        if database_id not in _desired_proxy_ids(node.id):
            await websocket.close(code=1008, reason="No linked app on this node uses this database")
            return
    await websocket.accept()
    client = db_proxy.ServerWebSocketStream(websocket)
    try:
        upstream = await open_db_stream(database_id)
    except Exception as e:
        log.warning("[db-relay] database %s for node %s: %s", database_id, node.name, e)
        await client.close()
        return
    await db_proxy.pipe(client, upstream)
