import asyncio
import collections
from collections import deque
import psutil
from fastapi import WebSocket, WebSocketDisconnect, APIRouter
from sqlalchemy import select
from core import auth
from containers import processes as pm
from core.database import AsyncSessionLocal
from core.models import Application, ApplicationReplica, Node
from nodes.registry import ensure_local_node
from apps.stream import _aggregate_frames, _open_remote_stream, _stream_local_container_stats

router = APIRouter(tags=["stats"])


_local_stats_history: deque = deque(maxlen=60)


@router.websocket("/ws/apps/{app_id}/stats")
async def stream_stats(app_id: int, websocket: WebSocket):
    if not await auth.authorize_websocket(websocket, "apps.view"):
        return
    await websocket.accept()

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Application).where(Application.id == app_id))
        app = result.scalar_one_or_none()
        if not app:
            # On a node, the local DB has no app rows — the primary owns them.
            # Stream the containers that run here instead (agent stats relay).
            await _stream_local_container_stats(websocket, app_id)
            return
        local_node = await ensure_local_node(db)

        # Collect all distinct nodes that have running replicas for this app
        rep_result = await db.execute(
            select(ApplicationReplica).where(
                ApplicationReplica.app_id == app_id,
                ApplicationReplica.status.in_(["running", "starting"]),
            )
        )
        replicas = rep_result.scalars().all()

        # Unique node IDs; None / local node are treated as local
        remote_node_ids = {r.node_id for r in replicas if r.node_id}
        nodes_result = await db.execute(select(Node).where(Node.id.in_(remote_node_ids)))
        all_nodes: dict[int, Node] = {n.id: n for n in nodes_result.scalars().all()}
        remote_nodes = [n for n in all_nodes.values() if not n.is_local]
        has_local = any(r.node_id is None or all_nodes.get(r.node_id, local_node).is_local for r in replicas)

        # Map node_id → list of replica IDs (for feeding per-replica stats on remote frames)
        replicas_per_node: dict[int | None, list[int]] = {}
        for r in replicas:
            key = r.node_id if r.node_id and not all_nodes.get(r.node_id, local_node).is_local else None
            replicas_per_node.setdefault(key, []).append(r.id)

    app_id_val = app.id
    app_name_val = app.name
    instance_count = len(replicas) if replicas else 1

    # ── Pure local case (fast path) ──────────────────────────────────────────
    local_replica_ids = replicas_per_node.get(None, [])
    if not remote_nodes and has_local:
        q = pm.subscribe_stats(app_id_val)
        for point in pm.get_recent_stats(app_id_val):
            if "cpu_percent" in point:
                point.setdefault("_instance_count", instance_count)
            try:
                await websocket.send_json(point)
            except Exception:
                pm.unsubscribe_stats(app_id_val, q)
                return
        try:
            while True:
                data = await q.get()
                if "cpu_percent" in data:
                    data.setdefault("_instance_count", instance_count)
                    # Keep per-replica stats fresh for single-replica local apps
                    if len(local_replica_ids) == 1:
                        pm.set_replica_stats(local_replica_ids[0], {**data, "replica_id": local_replica_ids[0]})
                await websocket.send_json(data)
        except (WebSocketDisconnect, Exception):
            pass
        finally:
            pm.unsubscribe_stats(app_id_val, q)
        return

    # ── Mixed / remote case — aggregate across nodes ─────────────────────────
    # latest frame per node; local node uses key None
    latest: dict = {}
    remote_q: asyncio.Queue = asyncio.Queue()
    tasks: list[asyncio.Task] = []

    for rn in remote_nodes:
        tasks.append(await _open_remote_stream(rn, app_id_val, app_name_val, remote_q))

    # Local node stats subscriber (if any local replicas)
    local_q = pm.subscribe_stats(app_id_val) if has_local else None

    # Flush local history to client immediately
    if local_q:
        for point in pm.get_recent_stats(app_id_val):
            if "cpu_percent" in point:
                latest[None] = point
        if latest:
            merged = _aggregate_frames(list(latest.values()))
            merged["_instance_count"] = instance_count
            pm._stats_history.setdefault(app_id_val, collections.deque(maxlen=60)).append(merged)
            try:
                await websocket.send_json(merged)
            except Exception:
                pass

    async def _drain_local():
        """Feed local stats queue into remote_q under key None."""
        if not local_q:
            return
        try:
            while True:
                data = await local_q.get()
                await remote_q.put((None, data))
        except asyncio.CancelledError:
            pass

    drain_task = asyncio.create_task(_drain_local())

    try:
        while True:
            try:
                node_id, frame = await asyncio.wait_for(remote_q.get(), timeout=10.0)
            except asyncio.TimeoutError:
                continue

            if frame.get("status") == "stopped":
                # Only propagate stopped if ALL nodes report stopped
                latest.pop(node_id, None)
                if not latest:
                    await websocket.send_json({"status": "stopped"})
                continue

            if "cpu_percent" not in frame:
                # Per-replica frame or metadata — pass through as-is
                await websocket.send_json(frame)
                continue

            latest[node_id] = frame
            # Store per-replica so the instances/stats endpoint can serve live data
            for rid in replicas_per_node.get(node_id, []):
                pm.set_replica_stats(rid, {**frame, "replica_id": rid})
            merged = _aggregate_frames(list(latest.values()))
            merged["_instance_count"] = instance_count
            # Buffer for history writer
            pm._stats_history.setdefault(app_id_val, collections.deque(maxlen=60)).append(merged)
            await websocket.send_json(merged)

    except (WebSocketDisconnect, Exception):
        pass
    finally:
        drain_task.cancel()
        for t in tasks:
            t.cancel()
        if local_q:
            pm.unsubscribe_stats(app_id_val, local_q)


@router.websocket("/ws/system/stats")
async def stream_system_stats(websocket: WebSocket):
    if not await auth.authorize_websocket(websocket, "apps.view"):
        return
    await websocket.accept()
    try:
        # Replay history so charts populate immediately on connect
        for snapshot in list(_local_stats_history):
            await websocket.send_json(snapshot)

        psutil.cpu_percent(interval=None)
        while True:
            import time
            timestamp = int(time.time() * 1000)  # milliseconds
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            cpu = await asyncio.to_thread(psutil.cpu_percent, 1)
            payload = {
                "timestamp": timestamp,
                "cpu_percent": cpu,
                "memory_total_mb": round(mem.total / 1024 / 1024),
                "memory_used_mb": round(mem.used / 1024 / 1024),
                "memory_percent": mem.percent,
                "disk_total_gb": round(disk.total / 1024 / 1024 / 1024, 1),
                "disk_used_gb": round(disk.used / 1024 / 1024 / 1024, 1),
                "disk_percent": disk.percent,
            }
            _local_stats_history.append(payload)
            await websocket.send_json(payload)
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        pass
