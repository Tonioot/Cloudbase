import logging
import threading
import time
from collections import deque
from typing import Optional
from containers.client import _get_client

log = logging.getLogger("cloudbase.docker")


def _is_loopback_bind_log(line: str, internal_port: Optional[int]) -> bool:
    low = (line or "").lower()
    if "127.0.0.1" not in low and "localhost" not in low:
        return False

    markers = (
        "running on",
        "listening on",
        "started server",
        "server running",
        "uvicorn running",
    )
    if not any(m in low for m in markers):
        return False

    if internal_port is None:
        return True
    return f":{internal_port}" in low


# ── Stats ─────────────────────────────────────────────────────────────────────

def attach_container_log_tailer(
    app_id: int,
    log_buffers: dict,
    push_line_fn,
    main_loop,
    cname: str,
) -> None:
    """Stream container logs to the in-memory buffer in a background thread."""
    if app_id not in log_buffers:
        log_buffers[app_id] = deque(maxlen=5000)

    resolved_cname = cname

    def _reader():
        try:
            for _ in range(40):
                try:
                    _get_client().containers.get(resolved_cname)
                    break
                except Exception:
                    pass
                time.sleep(0.25)

            client = _get_client()
            c = client.containers.get(resolved_cname)
            labels = (c.attrs.get("Config", {}) or {}).get("Labels", {}) or {}
            raw_internal_port = labels.get("cloudbase.internal_port")
            internal_port = int(raw_internal_port) if str(raw_internal_port or "").isdigit() else None
            warned_loopback_bind = False
            # Seed an empty buffer with recent history; otherwise only follow new
            # output, so re-attaching never replays the whole container log again.
            seed = 300 if not log_buffers[app_id] else 0
            for raw in c.logs(stream=True, follow=True, timestamps=False, tail=seed):
                line = raw.decode("utf-8", errors="replace").rstrip()
                log_buffers[app_id].append(line)
                if main_loop and not main_loop.is_closed():
                    main_loop.call_soon_threadsafe(
                        lambda l=line: None  # push_line_fn called below
                    )
                push_line_fn(app_id, line)
                if not warned_loopback_bind and _is_loopback_bind_log(line, internal_port):
                    warned_loopback_bind = True
                    target = f"port {internal_port}" if internal_port else "the exposed app port"
                    push_line_fn(
                        app_id,
                        f"[Docker] Warning: app appears to bind to localhost/127.0.0.1 inside the container for {target}. "
                        "Use host 0.0.0.0 so Docker port mapping and nginx can reach it.",
                    )
        except Exception as e:
            log.debug("[docker] Log tailer ended for app %d: %s", app_id, e)

    threading.Thread(target=_reader, daemon=True).start()


def get_recent_container_logs_by_name(container_name_str: str, lines: int = 300) -> list[str]:
    try:
        client = _get_client()
        c = client.containers.get(container_name_str)
        raw = c.logs(tail=lines, timestamps=False)
        return [l.decode("utf-8", errors="replace").rstrip() for l in raw.splitlines()]
    except Exception:
        return []


def get_container_stats_by_name(container_name_str: str) -> dict:
    """Resource use of one container, by name."""
    try:
        client = _get_client()
        c = client.containers.get(container_name_str)
        raw = c.stats(stream=False)

        cpu_delta = raw["cpu_stats"]["cpu_usage"]["total_usage"] - raw["precpu_stats"]["cpu_usage"]["total_usage"]
        system_delta = raw["cpu_stats"].get("system_cpu_usage", 0) - raw["precpu_stats"].get("system_cpu_usage", 0)
        num_cpus = raw["cpu_stats"].get("online_cpus") or len(raw["cpu_stats"]["cpu_usage"].get("percpu_usage") or [1])
        cpu_percent = (cpu_delta / system_delta * num_cpus * 100.0) if system_delta > 0 else 0.0

        mem_stats = raw.get("memory_stats", {})
        mem_usage = mem_stats.get("usage", 0)
        mem_inner = mem_stats.get("stats", {})
        if mem_usage == 0 and mem_inner:
            rss = mem_inner.get("anon", 0)
            mem_usage = rss + mem_inner.get("file", 0)
        else:
            mem_cache = mem_inner.get("inactive_file", mem_inner.get("cache", 0))
            rss = max(mem_usage - mem_cache, 0)

        c.reload()
        started_at = c.attrs.get("State", {}).get("StartedAt", "")
        uptime = 0
        if started_at:
            import datetime
            try:
                started = datetime.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                uptime = int((datetime.datetime.now(datetime.timezone.utc) - started).total_seconds())
            except Exception:
                pass

        networks = raw.get("networks") or {}
        net_rx = sum(v.get("rx_bytes", 0) for v in networks.values())
        net_tx = sum(v.get("tx_bytes", 0) for v in networks.values())

        blkio = raw.get("blkio_stats", {})
        io_list = blkio.get("io_service_bytes_recursive") or []
        disk_read  = sum(e.get("value", 0) for e in io_list if e.get("op", "").lower() == "read")
        disk_write = sum(e.get("value", 0) for e in io_list if e.get("op", "").lower() == "write")

        return {
            "cpu_percent": round(cpu_percent, 2),
            "memory_mb": round(rss / 1024 / 1024, 2),
            "memory_vms_mb": round(mem_usage / 1024 / 1024, 2),
            "uptime_seconds": uptime,
            "status": c.status,
            "num_threads": 0,
            "num_connections": 0,
            "net_rx_mb": round(net_rx / 1024 / 1024, 2),
            "net_tx_mb": round(net_tx / 1024 / 1024, 2),
            "disk_read_mb": round(disk_read / 1024 / 1024, 2),
            "disk_write_mb": round(disk_write / 1024 / 1024, 2),
        }
    except Exception as _e:
        try:
            import docker as _docker  # type: ignore
            if isinstance(_e, _docker.errors.NotFound):
                log.debug("get_container_stats_by_name: container not found: %s", container_name_str)
                return {}
        except ImportError:
            pass
        if isinstance(_e, ValueError):
            # Docker answers with an empty body while a container is being started or removed: no stats yet
            log.debug("get_container_stats_by_name: no stats yet for %s", container_name_str)
            return {}
        log.warning("get_container_stats_by_name failed for %s", container_name_str, exc_info=True)
        return {}


def aggregate_stats_frames(frames: list[dict]) -> dict:
    if not frames:
        return {"status": "stopped", "docker": True}

    def _sum(key: str) -> float:
        return round(sum((frame.get(key) or 0) for frame in frames), 2)

    def _avg(key: str) -> float:
        values = [frame.get(key) for frame in frames if frame.get(key) is not None]
        return round(sum(values) / len(values), 2) if values else 0.0

    def _max(key: str) -> Optional[float]:
        values = [frame.get(key) for frame in frames if frame.get(key) is not None]
        return max(values) if values else None

    return {
        "status": "running",
        "docker": True,
        "cpu_percent": _avg("cpu_percent"),
        "memory_mb": _sum("memory_mb"),
        "memory_vms_mb": _sum("memory_vms_mb"),
        "net_rx_mb": _sum("net_rx_mb"),
        "net_tx_mb": _sum("net_tx_mb"),
        "disk_read_mb": _sum("disk_read_mb"),
        "disk_write_mb": _sum("disk_write_mb"),
        "uptime_seconds": _max("uptime_seconds"),
    }
