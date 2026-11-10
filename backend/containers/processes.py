import asyncio
import os
import threading
from collections import deque
from typing import Optional

from core import activity, applog
from containers import naming as containers_naming
from containers import instances as containers_instances
from containers import stats_logs as containers_stats_logs

APPS_BASE_DIR = os.path.expanduser("~/.cloudbase/apps")

# Recent lines for history (capped, no tracking issues)
log_buffers: dict[int, deque] = {}

# Real-time subscribers: app_id -> list of asyncio.Queue
_log_queues: dict[int, list[asyncio.Queue]] = {}
_queues_lock = threading.Lock()

# Main event loop — set once at startup
_main_loop: Optional[asyncio.AbstractEventLoop] = None

# Stats history: last 60 snapshots per app (~2 min at 2s interval)
_stats_history: dict[int, deque] = {}
_stats_queues: dict[int, list[asyncio.Queue]] = {}
_stats_queues_lock = threading.Lock()

# Latest stats snapshot per instance_id (for the instances table)
_instance_stats: dict[int, dict] = {}


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _main_loop
    _main_loop = loop


def subscribe_stats(app_id: int) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    with _stats_queues_lock:
        _stats_queues.setdefault(app_id, []).append(q)
    return q


def unsubscribe_stats(app_id: int, q: asyncio.Queue) -> None:
    with _stats_queues_lock:
        queues = _stats_queues.get(app_id, [])
        try:
            queues.remove(q)
        except ValueError:
            pass


def _push_stat(app_id: int, data: dict) -> None:
    if _main_loop is None or _main_loop.is_closed():
        return
    with _stats_queues_lock:
        queues = list(_stats_queues.get(app_id, []))
    for q in queues:
        _main_loop.call_soon_threadsafe(q.put_nowait, data)


def get_recent_stats(app_id: int) -> list[dict]:
    return list(_stats_history.get(app_id, []))


def set_instance_stats(instance_id: int, data: dict) -> None:
    _instance_stats[instance_id] = data


def get_instance_stats(instance_id: int) -> dict | None:
    return _instance_stats.get(instance_id)


def get_all_instance_stats(app_id: int, instance_ids: list[int]) -> dict[int, dict]:
    return {rid: _instance_stats[rid] for rid in instance_ids if rid in _instance_stats}


def subscribe_logs(app_id: int) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    with _queues_lock:
        _log_queues.setdefault(app_id, []).append(q)
    return q


def unsubscribe_logs(app_id: int, q: asyncio.Queue) -> None:
    with _queues_lock:
        queues = _log_queues.get(app_id, [])
        try:
            queues.remove(q)
        except ValueError:
            pass


def _push_line(app_id: int, line: str) -> None:
    """Push a Cloudbase message to the app's live log and keep it in the app's Cloudbase log."""
    applog.record_push(app_id, line)
    activity.note(app_id, line)
    _push_container_line(app_id, line)


_STEPS = {
    "downloading": "downloading the source",
    "building_image": "building the image",
    "creating_container": "creating the container",
    "waiting_for_network": "waiting for the node network",
    "waiting": "waiting until it can be reached",
    "health_check": "health check",
}


def describe_instance(status: str, substatus: str | None = None, error: str | None = None) -> str:
    if status == "starting":
        return _STEPS.get(substatus or "", "starting")
    if status == "node_offline" and error:
        return f"its node cannot be reached ({error})"
    return {"running": "is live", "stopping": "stopping", "stopped": "stopped", "pending": "added",
            "node_offline": "its node cannot be reached",
            "error": f"failed: {error or 'unknown error'}"}.get(status, status)


def instance_line(app_id: int, instance_id: int, status: str, substatus: str | None = None, error: str | None = None) -> None:
    """What an instance is doing now, as the primary knows it (from its node's report or its own checks)."""
    _push_line(app_id, f"[Instance {instance_id}] {describe_instance(status, substatus, error)}")


def _push_container_line(app_id: int, line: str) -> None:
    """Push a line to the app's live log only (the app's own container output)."""
    if _main_loop is None or _main_loop.is_closed():
        return
    with _queues_lock:
        queues = list(_log_queues.get(app_id, []))
    for q in queues:
        _main_loop.call_soon_threadsafe(q.put_nowait, line)


def get_app_dir(app_id: int) -> str:
    return os.path.join(APPS_BASE_DIR, containers_naming.app_dir_name(app_id))


def app_source_dir(app) -> str | None:
    """The app's source directory, but only when it lies inside APPS_BASE_DIR.
    Anything else (an old or tampered working_dir) is never read, served or written."""
    base = os.path.realpath(APPS_BASE_DIR)
    path = os.path.realpath(app.working_dir or get_app_dir(app.id))
    if path == base or os.path.commonpath([base, path]) != base:
        return None
    return path


def detect_app_type_from_command(cmd: str) -> str:
    """Infer app type from the start command (used to name the Docker image label)."""
    cmd = cmd.strip().lower()
    if cmd.startswith("node ") or "npm " in cmd or cmd == "npm start" or cmd.startswith("npx "):
        return "nodejs"
    if cmd.startswith("python") or cmd.startswith("uvicorn") or cmd.startswith("gunicorn") or cmd.startswith("flask"):
        return "python"
    if cmd.startswith("ruby") or cmd.startswith("rails"):
        return "ruby"
    if cmd.startswith("go run") or cmd.startswith("go build"):
        return "go"
    if cmd.startswith("php") or cmd.startswith("composer"):
        return "php"
    if cmd.startswith("java") or cmd.startswith("mvn") or cmd.startswith("gradle"):
        return "java"
    if cmd.startswith("dotnet") or cmd.endswith(".exe"):
        return "dotnet"
    if cmd.startswith("cargo") or "target/release/" in cmd or "target/debug/" in cmd:
        return "rust"
    return "unknown"


def start_docker_instance(
    app_id: int,
    instance_id: int,
    internal_port: int,
    external_port: int,
    env_vars: dict,
    docker_options: dict | None = None,
) -> str:
    """Start a instance container using the existing image (no rebuild). Returns container ID."""
    def _push(aid, line):
        log_buffers.setdefault(aid, deque(maxlen=5000)).append(str(line))
        _push_line(aid, line)

    img = containers_naming.image_name(app_id)
    try:
        container_id = containers_instances.run_instance_container(
            app_id, instance_id, img,
            internal_port, external_port, env_vars or {}, docker_options, _push,
        )
    except Exception as e:
        message = str(e) or "Unknown Docker error"
        _push(app_id, f"[Instance] Start failed for instance {instance_id}: {message}")
        raise RuntimeError(message) from e

    if _main_loop is not None and not _main_loop.is_closed():
        containers_stats_logs.attach_container_log_tailer(
            app_id, log_buffers, _push_container_line, _main_loop,
            cname=containers_naming.instance_container_name(app_id, instance_id),
        )

    return container_id


def stop_docker_instance(app_id: int, instance_id: int) -> bool:
    """Stop and remove a instance container."""
    def _push(aid, line):
        log_buffers.setdefault(aid, deque(maxlen=5000)).append(str(line))
        _push_line(aid, line)

    return containers_instances.stop_instance_container(app_id, instance_id, push_line_fn=_push)


def get_recent_docker_logs(app_id: int, lines: int = 300) -> list[str]:
    """The last lines of the app's live log (kept in memory by the log tailers)."""
    return list(log_buffers.get(app_id) or [])[-lines:]
