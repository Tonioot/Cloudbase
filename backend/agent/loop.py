import json
import secrets
import socket
import asyncio
from typing import Optional
import httpx
import websockets
from agent.db_control import _db_control_loop
from agent.dispatch import _active_streams, _execute_command, _execute_in_background
from agent import alive, mesh as agent_mesh
from agent.log import _agent_log
from agent.reconcile import Reconciler
from agent.registration import _build_capabilities, _collect_node_metrics, _collect_system_info, _register, _report_result
from agent.state import AgentState, _load_state, _save_state
from agent.urls import _normalize_url, _ws_url

async def _maybe_upgrade_to_https(client: httpx.AsyncClient, state: AgentState, panel_url: Optional[str]) -> bool:
    """Registered over plain http while the primary now has an HTTPS address: move
    to it, so the node token, env vars and database traffic are encrypted. Only
    after the HTTPS address proves to be the same primary (it accepts our token)."""
    if not panel_url or not panel_url.startswith("https://") or state.main_url.startswith("https://"):
        return False
    new_url = _normalize_url(panel_url)
    try:
        r = await client.get(f"{new_url}/api/nodes/agent/desired",
                             headers={"X-Node-Token": state.auth_token}, timeout=15)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
    except Exception as e:
        _agent_log(f"[agent] Primary offers {new_url}, but it isn't usable from this node yet ({e}); staying on {state.main_url}")
        return False
    _agent_log(f"[agent] Switching the primary address from {state.main_url} to {new_url} (encrypted)")
    state.main_url = new_url
    _save_state(state)
    return True


# Told to the primary on every connect: a new value means this agent process started over, so whatever
# the old process was working on will never be answered
SESSION = secrets.token_hex(8)

_STREAM_COMMANDS = ("stream_logs", "stream_stats", "node_stats_stream")


async def _run_websocket_loop(client: httpx.AsyncClient, state: AgentState, reconciler: Reconciler):
    attempt = 0
    while True:
        ws_url = _ws_url(state.main_url)
        _agent_log(f"[agent] Connecting websocket to {ws_url} (attempt {attempt})")
        try:
            async with websockets.connect(ws_url, max_size=2**22, ping_interval=20, ping_timeout=20) as ws:
                attempt = 0
                alive.set_connected(True)
                _agent_log(f"[agent] WebSocket connected to {ws_url}")
                # Serialise all ws.send() calls — concurrent sends corrupt the connection
                _ws_lock = asyncio.Lock()

                async def _ws_send(payload: str):
                    async with _ws_lock:
                        await ws.send(payload)

                await _ws_send(json.dumps({"type": "auth", "token": state.auth_token, "session": SESSION}))

                async def _heartbeat_task():
                    while True:
                        await asyncio.sleep(state.heartbeat_interval)
                        await _ws_send(json.dumps({
                            "type": "heartbeat",
                            "node_metrics": _collect_node_metrics(),
                            "metadata_json": _collect_system_info(),
                            "capabilities": _build_capabilities()
                        }))

                hb_task = asyncio.create_task(_heartbeat_task())
                try:
                    async for message in ws:
                        data = json.loads(message)
                        mtype = data.get("type")

                        if mtype == "command":
                            cmd = data["command"]
                            if cmd.get("command_type") in _STREAM_COMMANDS:
                                await _execute_command(client, state, cmd, ws_main=ws, ws_send_fn=_ws_send)   # streams have no result to report
                            else:
                                asyncio.create_task(_execute_in_background(client, state, cmd))
                        elif mtype == "sync":
                            reconciler.nudge()          # the list of what to run changed
                        elif mtype == "cancel_stream":
                            sid = data.get("stream_id")
                            if sid and sid in _active_streams:
                                _active_streams[sid].cancel()
                                _active_streams.pop(sid, None)
                        elif mtype == "ping":
                            await _ws_send(json.dumps({"type": "pong", "ping_id": data.get("ping_id", "")}))
                        elif mtype == "auth_ok":
                            if await _maybe_upgrade_to_https(client, state, data.get("panel_url")):
                                break  # reconnect on the new address
                finally:
                    alive.set_connected(False)
                    hb_task.cancel()
                    try:
                        await hb_task
                    except asyncio.CancelledError:
                        pass
        except Exception as e:
            backoff = min(0.5 * (2 ** attempt), 60)
            _agent_log(f"[agent] websocket loop error (attempt {attempt}): {type(e).__name__}: {e} — retrying in {backoff:.1f}s")
            attempt += 1
            await asyncio.sleep(backoff)


async def start_agent(main_url=None, invite_code=None, node_name=None, public_host=None, heartbeat_interval=15, exit_after_registration=False):
    state = _load_state()
    if not state:
        # No saved state — must register
        if not main_url or not invite_code:
            _agent_log("[agent] No saved state and no registration args provided — cannot start")
            return
        async with httpx.AsyncClient() as client:
            state = await _register(client, _normalize_url(main_url), invite_code, node_name or socket.gethostname(), public_host, heartbeat_interval)
        _save_state(state)
        _agent_log(f"[agent] Registered node '{state.node_name}' (id={state.node_id})")
        if exit_after_registration:
            _agent_log("[agent] Registration complete, exiting as requested.")
            return
    elif exit_after_registration:
        # Already registered — nothing to do
        _agent_log(f"[agent] Already registered as '{state.node_name}' (id={state.node_id}), skipping re-registration")
        return
    
    db_control = asyncio.create_task(_db_control_loop(state))
    try:
        async with httpx.AsyncClient() as client:
            reconciler = Reconciler(client, state)
            background = [asyncio.create_task(agent_mesh.join_forever(client, state)), asyncio.create_task(reconciler.run())]
            try:
                await _drain_stale_commands(client, state)
                await _run_websocket_loop(client, state, reconciler)
            finally:
                for task in background:
                    task.cancel()
    finally:
        db_control.cancel()


async def _drain_stale_commands(client: httpx.AsyncClient, state: AgentState) -> None:
    """Fail any queued commands that were left over from a previous session."""
    headers = {"X-Node-Token": state.auth_token}
    try:
        resp = await client.get(
            f"{state.main_url}/api/nodes/agent/commands",
            headers=headers,
            timeout=10,
        )
        if resp.status_code != 200:
            return
        commands = resp.json().get("commands") or []
        for cmd in commands:
            _agent_log(f"[agent] Draining stale command {cmd['id']} ({cmd.get('command_type')})")
            await _report_result(client, state, cmd["id"], status="failed", error_message="Agent restarted — command discarded")
    except Exception as e:
        _agent_log(f"[agent] Could not drain stale commands: {e}")
