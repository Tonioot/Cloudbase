import json
import asyncio
import websockets
from dbs import manager as db_manager, proxy as db_proxy
from agent.log import _agent_log
from agent.state import AgentState
from agent.urls import _primary_ws_base

async def _open_db_proxy_ws(state: AgentState, database_id: int):
    """Upstream for a local database proxy: one binary WebSocket to the primary."""
    ws = await websockets.connect(
        f"{_primary_ws_base(state)}/api/nodes/ws/db-proxy/{database_id}",
        additional_headers={"x-node-token": state.auth_token},
        ping_interval=20, ping_timeout=30,
    )
    return db_proxy.ClientWebSocketStream(ws)


async def _db_dial(state: AgentState, msg: dict) -> None:
    """The primary asks for a connection to a database container on this node."""
    database_id, token = int(msg["database_id"]), msg["token"]
    ws = await websockets.connect(
        f"{_primary_ws_base(state)}/api/nodes/ws/db-dial/{token}",
        additional_headers={"x-node-token": state.auth_token},
        ping_interval=20, ping_timeout=30,
    )
    upstream = db_proxy.ClientWebSocketStream(ws)
    try:
        host, port = await asyncio.to_thread(db_manager.container_address, database_id)
        reader, writer = await asyncio.open_connection(host, port)
    except Exception as e:
        _agent_log(f"[db-dial] database {database_id}: {e}")
        await upstream.close()
        return
    await db_proxy.pipe(db_proxy.TcpStream(reader, writer), upstream)


async def _report_db_progress(ws) -> None:
    """Tell the primary what a database is doing before its container exists (an
    image pull can take minutes and blocks the command loop meanwhile)."""
    last = None
    while True:
        await asyncio.sleep(2)
        current = {str(k): v for k, v in db_manager.all_progress().items()}
        if current != last:
            await ws.send(json.dumps({"type": "progress", "progress": current}))
            last = current


async def _db_control_loop(state: AgentState) -> None:
    db_proxy.configure(asyncio.get_running_loop(), lambda db_id: _open_db_proxy_ws(state, db_id), state.node_id)
    url = f"{_primary_ws_base(state)}/api/nodes/ws/db-control"
    while True:
        try:
            async with websockets.connect(
                url, additional_headers={"x-node-token": state.auth_token}, ping_interval=20, ping_timeout=30,
            ) as ws:
                reporter = asyncio.create_task(_report_db_progress(ws))
                try:
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("type") == "proxies":
                            asyncio.create_task(db_proxy.reconcile(msg.get("database_ids") or []))
                        elif msg.get("type") == "dial":
                            asyncio.create_task(_db_dial(state, msg))
                finally:
                    reporter.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _agent_log(f"[db-control] disconnected ({type(e).__name__}: {e}), reconnecting in 5s")
        await asyncio.sleep(5)
