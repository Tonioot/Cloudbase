import asyncio
import os
import threading
from collections import deque
from typing import Optional

import docker_manager as dm

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

# Latest stats snapshot per replica_id (for the instances table)
_replica_stats: dict[int, dict] = {}


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


def set_replica_stats(replica_id: int, data: dict) -> None:
    _replica_stats[replica_id] = data


def get_replica_stats(replica_id: int) -> dict | None:
    return _replica_stats.get(replica_id)


def get_all_replica_stats(app_id: int, replica_ids: list[int]) -> dict[int, dict]:
    return {rid: _replica_stats[rid] for rid in replica_ids if rid in _replica_stats}


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
    if _main_loop is None or _main_loop.is_closed():
        return
    with _queues_lock:
        queues = list(_log_queues.get(app_id, []))
    for q in queues:
        _main_loop.call_soon_threadsafe(q.put_nowait, line)


def _safe_dir_name(name: str) -> str:
    import re
    return re.sub(r'[^a-zA-Z0-9_-]', '_', name)


def get_app_dir(app_name: str) -> str:
    return os.path.join(APPS_BASE_DIR, _safe_dir_name(app_name))


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
    return "unknown"


def start_docker_app(
    app_id: int,
    app_name: str,
    app_dir: str,
    internal_port: int,
    external_port: int,
    env_vars: dict,
    app_type: str,
    start_command: str,
    docker_options: dict | None = None,
    build: bool = True,
) -> str:
    """Build (if needed) and start a Docker container. Returns container ID."""
    log_buffers[app_id] = deque(maxlen=5000)
    _stats_history.pop(app_id, None)

    def _push(aid, line):
        log_buffers.setdefault(aid, deque(maxlen=5000)).append(str(line))
        _push_line(aid, line)

    try:
        if build:
            img = dm.build_image(app_id, app_name, app_dir, _push, app_type, start_command, internal_port)
        else:
            img = dm.image_name(app_id, app_name)

        container_id = dm.run_container(
            app_id, app_name, img, internal_port, external_port, env_vars or {}, docker_options, _push
        )
    except Exception as e:
        message = str(e) or "Unknown Docker error"
        _push(app_id, f"[Docker] Start failed: {message}")
        raise RuntimeError(message) from e

    if _main_loop is not None and not _main_loop.is_closed():
        dm.attach_container_log_tailer(app_id, log_buffers, _push_line, _main_loop)

    return container_id


def stop_docker_app(app_id: int) -> bool:
    """Stop and remove a Docker container."""
    def _push(aid, line):
        log_buffers.setdefault(aid, deque(maxlen=5000)).append(str(line))
        _push_line(aid, line)

    return dm.stop_container(app_id, push_line_fn=_push)


def start_docker_replica(
    app_id: int,
    replica_id: int,
    app_name: str,
    internal_port: int,
    external_port: int,
    env_vars: dict,
    docker_options: dict | None = None,
    image_app_id: int | None = None,
) -> str:
    """Start a replica container using the existing image (no rebuild). Returns container ID."""
    def _push(aid, line):
        log_buffers.setdefault(aid, deque(maxlen=5000)).append(str(line))
        _push_line(aid, line)

    img = dm.image_name(image_app_id if image_app_id is not None else app_id, app_name)
    try:
        container_id = dm.run_replica_container(
            app_id, replica_id, app_name, img,
            internal_port, external_port, env_vars or {}, docker_options, _push,
        )
    except Exception as e:
        message = str(e) or "Unknown Docker error"
        _push(app_id, f"[Replica] Start failed for replica {replica_id}: {message}")
        raise RuntimeError(message) from e

    if _main_loop is not None and not _main_loop.is_closed():
        dm.attach_container_log_tailer(
            app_id, log_buffers, _push_line, _main_loop,
            cname=dm.replica_container_name(app_id, replica_id),
        )

    return container_id


def stop_docker_replica(app_id: int, replica_id: int) -> bool:
    """Stop and remove a replica container."""
    def _push(aid, line):
        log_buffers.setdefault(aid, deque(maxlen=5000)).append(str(line))
        _push_line(aid, line)

    return dm.stop_replica_container(app_id, replica_id, push_line_fn=_push)


def is_docker_app_running(app_id: int) -> bool:
    return dm.is_container_running(app_id)


def get_docker_stats(app_id: int) -> dict:
    return dm.get_container_stats(app_id)


def get_recent_docker_logs(app_id: int, lines: int = 300) -> list[str]:
    buf = log_buffers.get(app_id)
    if buf:
        return list(buf)[-lines:]
    return dm.get_recent_container_logs(app_id, lines)
