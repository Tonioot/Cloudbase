import asyncio
from sqlalchemy.ext.asyncio import AsyncSession
from core.models import Application, Node
from containers import processes as pm
from containers import stats_logs as containers_stats_logs
from containers import naming as containers_naming
from apps.runtime import _load_app_replicas, _resolve_replica_node

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
            containers_stats_logs.get_container_stats_by_name,
            containers_naming.replica_container_name(app.id, replica.id),
        )
        if stats:
            frames.append(stats)
    return containers_stats_logs.aggregate_stats_frames(frames)


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
                containers_stats_logs.get_recent_container_logs_by_name,
                containers_naming.replica_container_name(app.id, replica.id),
                limit,
            )
        )
    return lines[-limit:]
