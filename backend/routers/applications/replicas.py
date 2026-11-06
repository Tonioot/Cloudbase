import asyncio
import json
import subprocess
from fastapi import Depends, Query, APIRouter, HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import get_db
from core.models import ApplicationReplica, Node
from containers import processes as pm, naming as containers_naming, stats_logs as containers_stats_logs
from nodes.commands import queue_node_command, wait_for_node_command
from core import auth as _auth
from apps.replica_stats import local_app_aggregate_stats
from apps.runtime import _load_node_map
from apps.serializers import _replica_to_dict, _get_or_404
from routers.applications.deps import _require_node_agent_for_app
from routers.applications.schemas import RunReplicaRequest, _SubstatusUpdate
from routers.applications.instances import delete_instance

router = APIRouter(prefix="/api/apps", tags=["applications"])


@router.post("/{app_id}/replicas/run-remote")
async def run_replica_remote(app_id: int, req: RunReplicaRequest, _node: None = Depends(_require_node_agent_for_app()), db: AsyncSession = Depends(get_db)):
    """Internal endpoint called by the node agent to start a replica container locally.
    The agent guarantees the image is already built before calling this endpoint.
    Does NOT create a DB row — the main server already has it."""
    # app_name is always supplied by the agent — no local DB lookup needed
    if req.app_name:
        app_name = req.app_name
    else:
        app = await _get_or_404(app_id, db)
        app_name = app.name

    # Image was built under local_app_id (= main_id on remote nodes)
    build_app_id = req.local_app_id if req.local_app_id is not None else app_id

    try:
        env_vars = req.env_vars or {}
        container_id = await asyncio.to_thread(
            pm.start_docker_replica,
            app_id, req.replica_id, app_name,
            req.internal_port, req.external_port,
            env_vars, req.docker_options,
            build_app_id,
        )
        return {"container_id": container_id, "replica_id": req.replica_id}
    except Exception as e:
        import logging as _logging
        _logging.getLogger("cloudbase.apps").error(
            "run-remote failed app_id=%d replica_id=%d local_app_id=%s build_app_id=%d: %s",
            app_id, req.replica_id, req.local_app_id, build_app_id, e, exc_info=True,
        )
        raise HTTPException(500, str(e)) from e


@router.delete("/{app_id}/replicas/{replica_id}/stop-remote")
async def stop_replica_remote(app_id: int, replica_id: int, _node: None = Depends(_require_node_agent_for_app())):
    """Internal endpoint called by the node agent to stop a replica container locally."""
    ok = await asyncio.to_thread(pm.stop_docker_replica, app_id, replica_id)
    return {"ok": ok, "replica_id": replica_id}


@router.patch("/{app_id}/replicas/{replica_id}/substatus")
async def update_replica_substatus(
    app_id: int,
    replica_id: int,
    body: _SubstatusUpdate,
    _node: None = Depends(_require_node_agent_for_app()),
    db: AsyncSession = Depends(get_db),
):
    """Internal endpoint called by node agents to report startup progress."""
    await db.execute(
        update(ApplicationReplica)
        .where(ApplicationReplica.id == replica_id, ApplicationReplica.app_id == app_id)
        .values(substatus=body.substatus)
    )
    await db.commit()
    return {"ok": True}


@router.get("/{app_id}/replicas/aggregate-stats")
async def get_replica_aggregate_stats(app_id: int, _node: None = Depends(_require_node_agent_for_app())):
    """Internal endpoint called by the node agent to collect stats across all local
    replica containers for app_id.  Returns aggregated cpu/memory/net/disk numbers
    plus status=running when at least one container is up."""
    return await local_app_aggregate_stats(app_id)


@router.get("/{app_id}/replicas/{replica_id}/stats-remote")
async def get_replica_stats_remote(app_id: int, replica_id: int, _node: None = Depends(_require_node_agent_for_app())):
    """Internal endpoint called by node-agent to get one replica container's stats.

    This avoids app-level aggregate ambiguity when replica mapping is in transition.
    """
    import time as _time
    cname = containers_naming.replica_container_name(app_id, replica_id)
    stats = await asyncio.to_thread(containers_stats_logs.get_container_stats_by_name, cname)
    if not stats or stats.get("status") != "running":
        return {"status": "stopped", "docker": True}
    stats["timestamp"] = int(_time.time() * 1000)
    stats["replica_id"] = replica_id
    return stats


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
