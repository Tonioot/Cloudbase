import asyncio
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from core.models import Application, Node
from containers import processes as pm, manager as dm
from apps.runtime import _load_app_replicas, _resolve_replica_node

def _aggregate_replica_stats_frames(frames: list[dict]) -> dict:
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


async def _get_local_replica_stats(
    app: Application,
    db: AsyncSession,
    local_node: Node,
) -> dict:
    replicas = await _load_app_replicas(app.id, db)
    frames: list[dict] = []
    for replica in replicas:
        replica_node = _resolve_replica_node(replica, {}, local_node)
        if replica_node is None or not replica_node.is_local:
            continue
        if replica.status != "running":
            continue
        stats = await asyncio.to_thread(
            dm.get_container_stats_by_name,
            dm.replica_container_name(app.id, replica.id),
        )
        if stats:
            frames.append(stats)
    return _aggregate_replica_stats_frames(frames)


async def _get_local_replica_logs(
    app: Application,
    db: AsyncSession,
    local_node: Node,
    limit: int,
) -> list[str]:
    buffered = list(pm.log_buffers.get(app.id, []))
    if buffered:
        return buffered[-limit:]

    replicas = await _load_app_replicas(app.id, db)
    lines: list[str] = []
    for replica in replicas:
        replica_node = _resolve_replica_node(replica, {}, local_node)
        if replica_node is None or not replica_node.is_local:
            continue
        lines.extend(
            await asyncio.to_thread(
                dm.get_recent_container_logs_by_name,
                dm.replica_container_name(app.id, replica.id),
                limit,
            )
        )
    return lines[-limit:]


async def local_app_aggregate_stats(app_id: int) -> dict:
    """Aggregate docker stats over this host's replica containers of app_id.

    Works without a DB row for the app, so node agents (whose own DB knows
    nothing about apps) can use it too.
    """
    import re as _re
    client = dm._get_client()
    pattern = _re.compile(rf"^cloudbase-app-{app_id}-replica-(\d+)$")
    try:
        containers = client.containers.list(filters={"name": f"cloudbase-app-{app_id}-replica-"})
    except Exception:
        return {"status": "stopped", "docker": True}

    running = [c for c in containers if pattern.match(c.name) and c.status == "running"]
    if not running:
        return {"status": "stopped", "docker": True}

    frames = []
    for c in running:
        s = await asyncio.to_thread(dm.get_container_stats_by_name, c.name)
        if s:
            frames.append(s)

    if not frames:
        return {"status": "stopped", "docker": True}

    def _sum(k):
        return round(sum(f.get(k) or 0 for f in frames), 2)

    def _avg(k):
        vals = [f[k] for f in frames if f.get(k) is not None]
        return round(sum(vals) / len(vals), 2) if vals else 0.0

    def _max(k):
        vals = [f[k] for f in frames if f.get(k) is not None]
        return max(vals) if vals else None

    return {
        "status": "running",
        "docker": True,
        "cpu_percent":    _avg("cpu_percent"),
        "memory_mb":      _sum("memory_mb"),
        "memory_vms_mb":  _sum("memory_vms_mb"),
        "net_rx_mb":      _sum("net_rx_mb"),
        "net_tx_mb":      _sum("net_tx_mb"),
        "disk_read_mb":   _sum("disk_read_mb"),
        "disk_write_mb":  _sum("disk_write_mb"),
        "uptime_seconds": _max("uptime_seconds"),
    }
