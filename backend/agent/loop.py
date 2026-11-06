import json
import socket
import asyncio
from typing import Optional
import httpx
import websockets
from agent.db_control import _db_control_loop
from agent.dispatch import _BACKGROUND_COMMANDS, _active_streams, _execute_command, _execute_in_background
from agent.log import _agent_log
from agent.orphans import _cleanup_orphaned_replica_containers
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
        r = await client.get(f"{new_url}/api/nodes/agent/my-replicas",
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


async def _run_websocket_loop(client: httpx.AsyncClient, state: AgentState):
    attempt = 0
    while True:
        ws_url = _ws_url(state.main_url)
        _agent_log(f"[agent] Connecting websocket to {ws_url} (attempt {attempt})")
        try:
            async with websockets.connect(ws_url, max_size=2**22, ping_interval=20, ping_timeout=20) as ws:
                attempt = 0
                _agent_log(f"[agent] WebSocket connected to {ws_url}")
                # Serialise all ws.send() calls — concurrent sends corrupt the connection
                _ws_lock = asyncio.Lock()

                async def _ws_send(payload: str):
                    async with _ws_lock:
                        await ws.send(payload)

                await _ws_send(json.dumps({"type": "auth", "token": state.auth_token}))

                async def _heartbeat_task():
                    await _cleanup_orphaned_replica_containers(client, state)
                    while True:
                        await asyncio.sleep(state.heartbeat_interval)
                        await _ws_send(json.dumps({
                            "type": "heartbeat",
                            "node_metrics": _collect_node_metrics(),
                            "metadata_json": _collect_system_info(),
                            "capabilities": _build_capabilities()
                        }))
                        _heartbeat_task._cleanup_tick = getattr(_heartbeat_task, "_cleanup_tick", 0) + 1
                        if _heartbeat_task._cleanup_tick % 5 == 0:
                            await _cleanup_orphaned_replica_containers(client, state)

                hb_task = asyncio.create_task(_heartbeat_task())
                try:
                    async for message in ws:
                        data = json.loads(message)
                        mtype = data.get("type")

                        if mtype == "command":
                            cmd = data["command"]
                            if cmd.get("command_type") in _BACKGROUND_COMMANDS:
                                asyncio.create_task(_execute_in_background(client, state, cmd))
                                continue
                            status, res, err = await _execute_command(client, state, cmd, ws_main=ws, ws_send_fn=_ws_send)
                            # Streaming commands use id=-1 and have no DB record — never ACK them
                            if status != "streaming" and cmd.get("id", -1) != -1:
                                await _ws_send(json.dumps({
                                    "type": "command_result",
                                    "command_id": cmd["id"],
                                    "status": status,
                                    "result": res,
                                    "error_message": err
                                }))
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
            await _drain_stale_commands(client, state)
            await _run_websocket_loop(client, state)
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
            # A stop is still wanted after a restart: it is how a replica deleted
            # while this node was offline gets its container removed. It is
            # idempotent, so run it instead of discarding it.
            if cmd.get("command_type") == "stop_replica":
                _agent_log(f"[agent] Running queued stop command {cmd['id']} from before restart")
                status, res, err = await _execute_command(client, state, cmd)
                await _report_result(client, state, cmd["id"], status=status, result=res, error_message=err)
                continue
            _agent_log(f"[agent] Draining stale command {cmd['id']} ({cmd.get('command_type')})")
            await _report_result(client, state, cmd["id"], status="failed", error_message="Agent restarted — command discarded")
    except Exception as e:
        _agent_log(f"[agent] Could not drain stale commands: {e}")
