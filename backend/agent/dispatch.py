import json
import secrets
import asyncio
from typing import Optional, Callable, Dict, Any
import httpx
from agent.commands.apps import cmd_cron_run, cmd_docker_cleanup, cmd_get_agent_logs, cmd_get_logs_tail, cmd_get_replica_logs, cmd_get_replica_stats, cmd_get_stats
from agent.commands.databases import cmd_db_backup, cmd_db_check_updates, cmd_db_ensure, cmd_db_logs, cmd_db_recreate, cmd_db_remove, cmd_db_restart, cmd_db_restore, cmd_db_rotate_password, cmd_db_states, cmd_db_stats, cmd_db_stats_many, cmd_db_stop, cmd_db_update
from agent.log import _agent_log
from agent.registration import _report_result
from agent.state import AgentState
from apps import node_streams

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
    "cron_run": cmd_cron_run,
}


async def _execute_in_background(client: httpx.AsyncClient, state: AgentState, cmd: dict) -> None:
    """Run one command beside the connection and report back over HTTP.

    Every command works this way, so a slow one (an image pull, a cron job, a cleanup) never holds up the
    others, and its answer still arrives when the websocket was replaced in the meantime."""
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

    try:
        # Streams run beside the connection and send their frames over it
        if command_type in ("stream_logs", "stream_stats", "node_stats_stream"):
            if not ws_main: return "failed", None, "Streaming requires WebSocket"
            stream_id = payload.get("stream_id") or secrets.token_hex(8)
            _agent_log(f"[agent] starting {command_type} relay: stream_id={stream_id}")
            # Use ws_send_fn (lock-protected) when available, fall back to raw ws_main.send
            send_fn = ws_send_fn if ws_send_fn is not None else ws_main.send
            task = asyncio.create_task(_stream_relay(send_fn, stream_id, command_type, int(main_id) if main_id else 0))
            _active_streams[stream_id] = task
            return "streaming", {"stream_id": stream_id}, None

        # Standard commands
        handler = COMMAND_HANDLERS.get(command_type)
        if not handler:
            _agent_log(f"[agent] Unsupported command: {command_type}")
            return "failed", None, f"Unsupported command type: {command_type!r}"
        
        result = await handler(client, state, main_id, payload)
        return "done", result, None

    except Exception as e:
        _agent_log(f"[agent] Command {command_type} failed: {e}")
        return "failed", None, str(e)


# ─── WebSocket / Loops ────────────────────────────────────────────────────────

_active_streams: dict[str, asyncio.Task] = {}


_pending_pings: dict[str, asyncio.Future] = {}


async def _stream_relay(send_fn, stream_id: str, kind: str, app_id: int) -> None:
    """Send the frames of a stream to the primary until it is cancelled."""
    frames = 0

    async def send_text(text: str) -> None:
        nonlocal frames
        frames += 1
        await send_fn(json.dumps({"type": "stream_data", "stream_id": stream_id, "data": text}))
        if frames == 1:
            _agent_log(f"[stream] {stream_id} first frame sent")

    async def send_frame(frame: dict) -> None:
        await send_text(json.dumps(frame))

    try:
        if kind == "stream_logs":
            await node_streams.follow_container_logs(app_id, send_text)
        elif kind == "stream_stats":
            await node_streams.stream_container_stats(app_id, send_frame)
        else:
            await node_streams.stream_system_stats(send_frame)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        _agent_log(f"[stream] {stream_id} error after {frames} frames: {type(e).__name__}: {e}")
    finally:
        _active_streams.pop(stream_id, None)
        _agent_log(f"[stream] {stream_id} relay ended ({frames} frames total)")
