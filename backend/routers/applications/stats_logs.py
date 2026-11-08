import json
from fastapi import Query, Depends, APIRouter
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import get_db
from containers import processes as pm
from nodes.registry import ensure_local_node
from nodes.commands import wait_for_node_command, queue_node_command
from core import auth as _auth
from apps.replica_stats import _get_local_replica_logs, _get_local_replica_stats
from apps.runtime import _get_app_node
from apps.serializers import _get_or_404

router = APIRouter(prefix="/api/apps", tags=["applications"])


@router.get("/{app_id}/stats")
async def get_stats(app_id: int, _user: dict = Depends(_auth.require_permission("apps.view")), db: AsyncSession = Depends(get_db)):
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)
    node = await _get_app_node(app, db, local_node)
    if not node.is_local:
        if node.status != "online":
            return {
                "status": "stopped",
                "remote": True,
                "node": {
                    "id": node.id,
                    "name": node.name,
                    "status": node.status,
                    "is_local": bool(node.is_local),
                },
            }
        cmd = await queue_node_command(
            db,
            node_id=node.id,
            app_id=app.id,
            command_type="get_stats",
            payload={"app_id": app.id, "app_name": app.name},
            allow_existing_inflight=True,
        )
        done = await wait_for_node_command(db, cmd.id, timeout_seconds=20)
        if done.status != "done":
            return {
                "status": "stopped",
                "remote": True,
                "error": done.error_message,
                "node": {
                    "id": node.id,
                    "name": node.name,
                    "status": node.status,
                    "is_local": bool(node.is_local),
                },
            }
        data = json.loads(done.result or "{}") if done.result else {}
        data["remote"] = True
        data["node"] = {
            "id": node.id,
            "name": node.name,
            "status": node.status,
            "is_local": bool(node.is_local),
        }
        return data

    replica_stats = await _get_local_replica_stats(app, db, local_node)
    if replica_stats.get("status") == "running":
        return replica_stats

    return {"status": "stopped", "docker": True}


@router.get("/{app_id}/logs/tail")
async def get_logs_tail(app_id: int, limit: int = Query(200, ge=1, le=2000), db: AsyncSession = Depends(get_db)):
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)
    node = await _get_app_node(app, db, local_node)

    if not node.is_local:
        if node.status != "online":
            return {"lines": [], "remote": True, "error": "Node is offline"}
        cmd = await queue_node_command(
            db,
            node_id=node.id,
            app_id=app.id,
            command_type="get_logs_tail",
            payload={"app_id": app.id, "app_name": app.name, "limit": limit},
        )
        done = await wait_for_node_command(db, cmd.id, timeout_seconds=20)
        if done.status != "done":
            return {"lines": [], "remote": True, "error": done.error_message}
        result_payload = json.loads(done.result or "{}") if done.result else {}
        return {"lines": result_payload.get("lines", []) or [], "remote": True}

    replica_lines = await _get_local_replica_logs(app, db, local_node, limit)
    if replica_lines:
        return {"lines": replica_lines, "remote": False}

    lines = pm.get_recent_docker_logs(app_id, limit)
    return {"lines": lines, "remote": False}
