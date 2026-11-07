import asyncio
import json
import subprocess
from fastapi import Depends, Query, APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import get_db
from core.models import ApplicationReplica, Node
from containers import naming as containers_naming
from nodes.commands import queue_node_command, wait_for_node_command
from core import auth as _auth
from apps.runtime import _load_node_map
from apps.serializers import _replica_to_dict, _get_or_404
from routers.applications.instances import delete_instance

router = APIRouter(prefix="/api/apps", tags=["applications"])


@router.get("/{app_id}/replicas")
async def list_replicas(app_id: int, _user: dict = Depends(_auth.require_permission("apps.view")), db: AsyncSession = Depends(get_db)):
    await _get_or_404(app_id, db)
    node_map = await _load_node_map(db)
    result = await db.execute(
        select(ApplicationReplica).where(ApplicationReplica.app_id == app_id)
    )
    replicas = result.scalars().all()
    return [_replica_to_dict(r, node_map.get(r.node_id)) for r in replicas]


@router.get("/{app_id}/replicas/{replica_id}/logs")
async def get_replica_logs(
    app_id: int,
    replica_id: int,
    lines: int = Query(200, ge=1, le=2000),
    db: AsyncSession = Depends(get_db),
):
    """Fetch recent log lines for a specific replica container.

    For local replicas: reads directly from docker logs.
    For remote replicas: queues a get_logs_tail agent command with the container name.
    """
    app = await _get_or_404(app_id, db)
    rep_result = await db.execute(
        select(ApplicationReplica).where(
            ApplicationReplica.id == replica_id,
            ApplicationReplica.app_id == app_id,
        )
    )
    replica = rep_result.scalar_one_or_none()
    if not replica:
        raise HTTPException(404, "Replica not found")

    node_result = await db.execute(select(Node).where(Node.id == replica.node_id)) if replica.node_id else None
    rep_node = (node_result.scalar_one_or_none() if node_result else None)

    container_name = containers_naming.replica_container_name(app_id, replica_id)

    if rep_node is None or rep_node.is_local:
        # Local — read directly
        try:
            raw = await asyncio.to_thread(
                lambda: subprocess.check_output(
                    ["docker", "logs", "--tail", str(lines), container_name],
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            )
            return {"lines": raw.splitlines(), "remote": False}
        except Exception as e:
            return {"lines": [], "remote": False, "error": str(e)}

    if rep_node.status != "online":
        return {"lines": [], "remote": True, "error": "Node is offline"}

    cmd = await queue_node_command(
        db,
        node_id=rep_node.id,
        app_id=app_id,
        command_type="get_replica_logs",
        payload={
            "app_id": app_id,
            "app_name": app.name,
            "replica_id": replica_id,
            "container_name": container_name,
            "lines": lines,
        },
    )
    done = await wait_for_node_command(db, cmd.id, timeout_seconds=20)
    if done.status != "done":
        return {"lines": [], "remote": True, "error": done.error_message}
    result_payload = json.loads(done.result or "{}") if done.result else {}
    return {"lines": result_payload.get("lines", []) or [], "remote": True}


@router.delete("/{app_id}/replicas/{replica_id}")
async def remove_replica(app_id: int, replica_id: int, db: AsyncSession = Depends(get_db), actor: str = Depends(_auth.get_current_actor)):
    """Alias for DELETE /instances/{id} — kept for backwards compatibility."""
    return await delete_instance(app_id, replica_id, db=db, actor=actor)
