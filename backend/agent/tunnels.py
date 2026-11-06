import json
import base64
import asyncio
import websockets
from agent.log import _agent_log
from agent.state import AgentState
from agent.urls import _tunnel_ws_url

# ─── Tunnel client state ──────────────────────────────────────────────────────
# replica_id → running asyncio.Task (the reconnecting tunnel loop)
_active_tunnels: dict[int, asyncio.Task] = {}


async def _tunnel_relay(ws, local_port: int) -> None:
    """Relay TCP connections from the main node's TCP listener to the local replica.

    The main node sends:
      {"type": "connect", "conn_id": "..."}   — open TCP conn to 127.0.0.1:local_port
      {"type": "data",    "conn_id": "...", "data": "<base64>"}
      {"type": "close",   "conn_id": "..."}

    This side responds with:
      {"type": "data",   "conn_id": "...", "data": "<base64>"}
      {"type": "close",  "conn_id": "..."}
    """
    conns: dict[str, tuple[asyncio.StreamReader, asyncio.StreamWriter]] = {}

    async def _forward_to_ws(conn_id: str, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                await ws.send(json.dumps({
                    "type": "data",
                    "conn_id": conn_id,
                    "data": base64.b64encode(chunk).decode(),
                }))
        except Exception:
            pass
        finally:
            try:
                await ws.send(json.dumps({"type": "close", "conn_id": conn_id}))
            except Exception:
                pass
            pair = conns.pop(conn_id, None)
            if pair:
                try:
                    pair[1].close()
                except Exception:
                    pass

    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            mtype   = msg.get("type")
            conn_id = msg.get("conn_id")
            if not conn_id:
                continue

            if mtype == "connect":
                try:
                    reader, writer = await asyncio.open_connection("127.0.0.1", local_port)
                    conns[conn_id] = (reader, writer)
                    asyncio.get_running_loop().create_task(_forward_to_ws(conn_id, reader))
                except Exception as e:
                    _agent_log(f"[tunnel] connect to 127.0.0.1:{local_port} failed ({e})")
                    try:
                        await ws.send(json.dumps({"type": "close", "conn_id": conn_id}))
                    except Exception:
                        pass

            elif mtype == "data":
                pair = conns.get(conn_id)
                if pair:
                    _, writer = pair
                    try:
                        writer.write(base64.b64decode(msg["data"]))
                        await writer.drain()
                    except Exception:
                        conns.pop(conn_id, None)

            elif mtype == "close":
                pair = conns.pop(conn_id, None)
                if pair:
                    try:
                        pair[1].close()
                    except Exception:
                        pass
    finally:
        for _, writer in list(conns.values()):
            try:
                writer.close()
            except Exception:
                pass
        conns.clear()


async def _run_tunnel(state: AgentState, replica_id: int, local_port: int) -> None:
    """Persistent reconnecting tunnel loop for one replica.

    Connects to the main node's /api/nodes/ws/tunnel/{replica_id} and relays
    TCP connections until the task is cancelled.
    """
    # Brief delay so the command-result HTTP response arrives at the main node
    # before the tunnel WebSocket connects (avoids a DB race on replica.status).
    await asyncio.sleep(0.5)

    tunnel_url = _tunnel_ws_url(state.main_url, replica_id)
    while True:
        try:
            _agent_log(f"[tunnel] replica={replica_id} connecting to {tunnel_url}")
            async with websockets.connect(
                tunnel_url,
                additional_headers={"x-node-token": state.auth_token},
                ping_interval=20,
                ping_timeout=30,
                max_size=2 ** 22,
            ) as ws:
                _agent_log(f"[tunnel] replica={replica_id} connected, relaying to 127.0.0.1:{local_port}")
                await _tunnel_relay(ws, local_port)
        except asyncio.CancelledError:
            _agent_log(f"[tunnel] replica={replica_id} cancelled")
            break
        except Exception as e:
            # The primary rejects the tunnel (HTTP 403 / close 1008) when the
            # replica no longer exists or isn't ours — stop instead of retrying forever.
            status = getattr(getattr(e, "response", None), "status_code", None) or getattr(e, "status_code", None)
            close_code = getattr(getattr(e, "rcvd", None), "code", None)
            if status == 403 or close_code == 1008:
                _agent_log(f"[tunnel] replica={replica_id} rejected by primary ({e}), stopping tunnel")
                if _active_tunnels.get(replica_id) is asyncio.current_task():
                    _active_tunnels.pop(replica_id, None)
                break
            _agent_log(f"[tunnel] replica={replica_id} disconnected ({type(e).__name__}: {e}), reconnecting in 5s")

        # Check if tunnel was explicitly removed before reconnecting
        if replica_id not in _active_tunnels:
            break
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            break


def _start_tunnel_task(state: AgentState, replica_id: int, local_port: int) -> None:
    """Register and start a persistent tunnel task for a replica."""
    old = _active_tunnels.pop(replica_id, None)
    if old and not old.done():
        old.cancel()
    task = asyncio.get_running_loop().create_task(
        _run_tunnel(state, replica_id, local_port)
    )
    _active_tunnels[replica_id] = task
    _agent_log(f"[tunnel] replica={replica_id} task started (port={local_port})")


async def _stop_tunnel_task(replica_id: int) -> None:
    """Cancel and await the tunnel task for a replica."""
    task = _active_tunnels.pop(replica_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=3.0)
        except Exception:
            pass
    _agent_log(f"[tunnel] replica={replica_id} task stopped")
