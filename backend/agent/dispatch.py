import json
import secrets
import asyncio
from typing import Optional, Callable, Dict, Any
import httpx
import websockets
from agent.commands.apps import cmd_cron_run, cmd_delete_app, cmd_docker_cleanup, cmd_get_agent_logs, cmd_get_logs_tail, cmd_get_replica_logs, cmd_get_replica_stats, cmd_get_stats, cmd_refresh_source, cmd_start_replica, cmd_stop_replica
from agent.commands.databases import cmd_db_backup, cmd_db_check_updates, cmd_db_ensure, cmd_db_logs, cmd_db_recreate, cmd_db_remove, cmd_db_restart, cmd_db_restore, cmd_db_rotate_password, cmd_db_states, cmd_db_stats, cmd_db_stats_many, cmd_db_stop, cmd_db_update
from agent.log import _agent_log
from agent.registration import _report_result
from agent.state import AgentState, _load_agent_token
from agent.urls import _local_ws_url

# ─── Command Dispatcher ───────────────────────────────────────────────────────

COMMAND_HANDLERS: Dict[str, Callable] = {
    "db_ensure": cmd_db_ensure,
    "db_recreate": cmd_db_recreate,
    "db_restart": cmd_db_restart,
    "db_stop": cmd_db_stop,
    "db_remove": cmd_db_remove,
    "db_states": cmd_db_states,
    "db_stats": cmd_db_stats,
    "db_logs": cmd_db_logs,
    "db_backup": cmd_db_backup,
    "db_restore": cmd_db_restore,
    "db_rotate_password": cmd_db_rotate_password,
    "db_stats_many": cmd_db_stats_many,
    "db_check_updates": cmd_db_check_updates,
    "db_update": cmd_db_update,
    "get_logs_tail": cmd_get_logs_tail,
    "get_replica_logs": cmd_get_replica_logs,
    "get_stats": cmd_get_stats,
    "docker_cleanup": cmd_docker_cleanup,
    "get_replica_stats": cmd_get_replica_stats,
    "get_agent_logs": cmd_get_agent_logs,
    "delete_app": cmd_delete_app,
    "start_replica": cmd_start_replica,
    "cron_run": cmd_cron_run,
    "stop_replica": cmd_stop_replica,
    "refresh_source": cmd_refresh_source,
}


# Commands that can take hours (a cron job): they run beside the command loop,
# so the node keeps answering other commands, and report back over HTTP.
_BACKGROUND_COMMANDS = {"cron_run"}


async def _execute_in_background(client: httpx.AsyncClient, state: AgentState, cmd: dict) -> None:
    status, res, err = await _execute_command(client, state, cmd)
    for attempt in range(5):  # the primary may be restarting
        try:
            await _report_result(client, state, cmd["id"], status=status, result=res, error_message=err)
            return
        except Exception as e:
            _agent_log(f"[agent] Could not report result of command {cmd['id']} (attempt {attempt + 1}): {e}")
            await asyncio.sleep(10 * (attempt + 1))


async def _execute_command(
    client: httpx.AsyncClient,
    state: AgentState,
    command: dict[str, Any],
    ws_main=None,
    ws_send_fn=None,
) -> tuple[str, Optional[dict[str, Any]], Optional[str]]:
    command_type = command.get("command_type") or "unknown"
    payload = command.get("payload") or {}
    main_id = str(command.get("app_id") or payload.get("app_id") or "")
    
    agent_token = _load_agent_token()
    headers = {"X-Agent-Token": agent_token, "Content-Type": "application/json"}

    try:
        # Handle streaming separately as they need ws_main
        if command_type in ("stream_logs", "stream_stats", "node_stats_stream"):
            if not ws_main: return "failed", None, "Streaming requires WebSocket"

            if command_type == "node_stats_stream":
                local_path = "/ws/system/stats"
            else:
                # main_id is used directly — no local DB lookup needed
                suffix = "logs" if command_type == "stream_logs" else "stats"
                local_path = f"/ws/apps/{main_id}/{suffix}"

            stream_id = payload.get("stream_id") or secrets.token_hex(8)
            _agent_log(f"[agent] starting {command_type} relay: local={local_path} stream_id={stream_id}")
            # Use ws_send_fn (lock-protected) when available, fall back to raw ws_main.send
            send_fn = ws_send_fn if ws_send_fn is not None else ws_main.send
            task = asyncio.create_task(_stream_relay(send_fn, stream_id, local_path))
            _active_streams[stream_id] = task
            return "streaming", {"stream_id": stream_id}, None

        # Standard commands
        handler = COMMAND_HANDLERS.get(command_type)
        if not handler:
            _agent_log(f"[agent] Unsupported command: {command_type}")
            return "failed", None, f"Unsupported command type: {command_type!r}"
        
        result = await handler(client, state, main_id, payload, headers)
        return "done", result, None

    except Exception as e:
        _agent_log(f"[agent] Command {command_type} failed: {e}")
        return "failed", None, str(e)


# ─── WebSocket / Loops ────────────────────────────────────────────────────────

_active_streams: dict[str, asyncio.Task] = {}


_pending_pings: dict[str, asyncio.Future] = {}


async def _stream_relay(send_fn, stream_id: str, local_ws_path: str) -> None:
    local_url = _local_ws_url(local_ws_path)
    agent_token = _load_agent_token()
    extra_headers = {"X-Agent-Token": agent_token} if agent_token else {}
    frames = 0
    try:
        _agent_log(f"[stream] {stream_id} connecting to {local_url}")
        async with websockets.connect(local_url, max_size=2**22, additional_headers=extra_headers) as local_ws:
            _agent_log(f"[stream] {stream_id} connected, relaying to main WS")
            async for raw in local_ws:
                frames += 1
                await send_fn(json.dumps({
                    "type": "stream_data",
                    "stream_id": stream_id,
                    "data": raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace"),
                }))
                if frames == 1:
                    _agent_log(f"[stream] {stream_id} first frame sent")
    except Exception as e:
        _agent_log(f"[stream] {stream_id} error after {frames} frames: {type(e).__name__}: {e}")
    finally:
        _active_streams.pop(stream_id, None)
        _agent_log(f"[stream] {stream_id} relay ended ({frames} frames total)")
