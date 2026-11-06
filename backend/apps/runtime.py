import asyncio
from typing import Optional
from fastapi import HTTPException
from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import AsyncSessionLocal
import datetime as _dt
from core.models import Deployment, NodeCommand, StatsHistory, ApplicationReplica, Node, Application, AuditLog
from containers import processes as pm
from containers import naming as containers_naming
from containers import build as containers_build
from containers import replicas as containers_replicas
from core import applog
from dbs import links as db_links
from nodes.registry import ensure_local_node
from nodes.commands import queue_node_command
from apps.git import _decrypt_github_token, _refresh_app_source_revision
from apps.images import _image_build_kwargs, _image_port
from apps.nginx_sync import _has_public_nginx_domain

async def _cleanup_app_dependencies(db: AsyncSession, app_id: int) -> None:
    """Remove or detach rows that can still reference an app in older DB schemas."""
    # App ids can be reused; a new app must not inherit this one's Cloudbase log.
    applog.forget(app_id)
    await db.execute(
        update(NodeCommand)
        .where(NodeCommand.app_id == app_id)
        .values(app_id=None)
    )
    await db.execute(
        update(AuditLog)
        .where(AuditLog.app_id == app_id)
        .values(app_id=None)
    )
    await db.execute(delete(StatsHistory).where(StatsHistory.app_id == app_id))
    await db.execute(delete(ApplicationReplica).where(ApplicationReplica.app_id == app_id))
    await db.execute(delete(Deployment).where(Deployment.app_id == app_id))


def _docker_runtime_options(app: Application) -> dict:
    return {
        "cpu_limit": app.docker_cpu_limit,
        "memory_limit_mb": app.docker_memory_limit_mb,
        "read_only_root": bool(app.docker_read_only_root),
        "tmpfs_enabled": bool(app.docker_tmpfs_enabled),
        "tmpfs_size_mb": app.docker_tmpfs_size_mb,
        "restart_policy": app.restart_policy or "no",
        # Reached through nginx (or the node tunnel), both on localhost: then the
        # host port must not be open to the internet as well. Without a domain
        # the host port is the only way in, so it stays public.
        "bind_host": "127.0.0.1" if _has_public_nginx_domain(app) else None,
        # Workers have no web server: nothing to reach, so no port on the host
        "publish_port": not app.no_web,
        **db_links.runtime_options(app.id),
    }


async def _load_app_replicas(app_id: int, db: AsyncSession) -> list[ApplicationReplica]:
    result = await db.execute(
        select(ApplicationReplica).where(ApplicationReplica.app_id == app_id)
    )
    return result.scalars().all()


def _resolve_replica_node(
    replica: ApplicationReplica,
    node_map: dict[int, Node],
    local_node: Node,
) -> Optional[Node]:
    if replica.node_id is None or replica.node_id == local_node.id:
        return local_node
    return node_map.get(replica.node_id)


async def _best_effort_stop_legacy_app_container(app_id: int) -> None:
    if await asyncio.to_thread(pm.is_docker_app_running, app_id):
        await asyncio.to_thread(pm.stop_docker_app, app_id)


def _remote_replica_command_payload(app: Application, env_vars: dict, external_port: int, source_revision: Optional[str] = None) -> dict:
    if source_revision is None:
        source_revision = _refresh_app_source_revision(app)
    app_type = app.app_type or pm.detect_app_type_from_command(app.start_command or "")
    return {
        "app_id": app.id,
        "app_name": app.name,
        "repo_url": app.repo_url,
        "github_token": _decrypt_github_token(app.github_token),
        "app_type": app_type or "unknown",
        "start_command": app.start_command,
        "build_command": app.build_command or "",
        "internal_port": _image_port(app),
        "external_port": external_port,
        "env_vars": env_vars,
        "restart_policy": app.restart_policy,
        "docker_cpu_limit": app.docker_cpu_limit,
        "docker_memory_limit_mb": app.docker_memory_limit_mb,
        "docker_read_only_root": bool(app.docker_read_only_root),
        "docker_tmpfs_enabled": bool(app.docker_tmpfs_enabled),
        "docker_tmpfs_size_mb": app.docker_tmpfs_size_mb,
        "docker_options": _docker_runtime_options(app),
        "source_revision": source_revision,
    }


async def _sync_process_status(app, db) -> None:
    """Reconcile DB status with actual OS state. Uses port recovery as fallback."""
    if app.cron_schedule:
        return  # no instances: the scheduler keeps a cron job's status
    local_node = await ensure_local_node(db)
    node = await _get_app_node(app, db, local_node)
    if not node.is_local:
        return

    # Instance-based model: sync each local replica's container status
    replica_result = await db.execute(
        select(ApplicationReplica).where(ApplicationReplica.app_id == app.id)
    )
    replicas = replica_result.scalars().all()
    if replicas:
        changed = False
        for replica in replicas:
            # Skip non-local replicas (their status is managed by the remote node)
            if replica.node_id:
                rn_result = await db.execute(select(Node).where(Node.id == replica.node_id))
                r_node = rn_result.scalar_one_or_none()
                if r_node is not None and not r_node.is_local:
                    continue
            # Timeout stuck deploying/starting instances (> 10 min = something went wrong)
            if replica.status in ("deploying", "starting"):
                age = (_dt.datetime.utcnow() - (replica.updated_at or replica.created_at)).total_seconds()
                if age > 600:
                    replica.status = "error"
                    replica.substatus = None
                    replica.last_error = f"Timed out after {int(age)}s in '{replica.status}' state"
                    changed = True
                continue
            if replica.status == "stopping":
                continue
            # Grace period: skip container check for replicas that just became "running"
            # to avoid a false "stopped" flash while Docker transitions from "created" to "running".
            if replica.status == "running":
                age = (_dt.datetime.utcnow() - (replica.updated_at or replica.created_at)).total_seconds()
                if age < 15:
                    continue
            alive = await asyncio.to_thread(containers_replicas.is_replica_container_running, app.id, replica.id)
            new_status = "running" if alive else "stopped"
            if replica.status != new_status:
                replica.status = new_status
                if new_status in ("running", "stopped"):
                    replica.substatus = None
                changed = True
        new_app_status = _derive_app_status_from_instances(replicas)
        if app.status != new_app_status:
            app.status = new_app_status
            changed = True
        if changed:
            await db.commit()
        return

    # Legacy single-container model
    alive = await asyncio.to_thread(pm.is_docker_app_running, app.id)
    new_status = "running" if alive else "stopped"
    if app.status != new_status:
        app.status = new_status
        await db.commit()


async def _set_replica_substatus(replica_id: int, substatus: Optional[str]) -> None:
    """Write substatus to DB for a replica — fire and forget, never raises."""
    try:
        async with AsyncSessionLocal() as _db:
            await _db.execute(
                update(ApplicationReplica)
                .where(ApplicationReplica.id == replica_id)
                .values(substatus=substatus)
            )
            await _db.commit()
    except Exception:
        pass


async def _start_instance_local(app: "Application", replica: "ApplicationReplica", env_vars: dict, app_id: int) -> str:
    """Start a local replica container, building the Docker image first if needed."""
    desired_revision = await asyncio.to_thread(_refresh_app_source_revision, app)
    docker_opts = _docker_runtime_options(app)
    # Per-instance overrides
    if replica.docker_cpu_limit is not None:
        docker_opts["cpu_limit"] = replica.docker_cpu_limit
    if replica.docker_memory_limit_mb is not None:
        docker_opts["memory_limit_mb"] = replica.docker_memory_limit_mb
    if replica.docker_read_only_root:
        docker_opts["read_only_root"] = True
    if replica.docker_tmpfs_enabled:
        docker_opts["tmpfs_enabled"] = True
    if replica.docker_tmpfs_size_mb is not None:
        docker_opts["tmpfs_size_mb"] = replica.docker_tmpfs_size_mb

    async def _run_replica(ext_port: int) -> str:
        await _set_replica_substatus(replica.id, "creating_container")
        return await asyncio.to_thread(
            pm.start_docker_replica,
            app_id, replica.id, app.name,
            _image_port(app),
            ext_port,
            env_vars, docker_opts,
            app_id,  # image_app_id
        )

    async def _build_image() -> None:
        if not app.working_dir:
            raise HTTPException(400, "No working directory — deploy the app first")
        def _push(_aid, line):
            pm._push_line(app_id, str(line))
        await _set_replica_substatus(replica.id, "building_image")
        await asyncio.to_thread(
            containers_build.build_image,
            app_id, app.name, app.working_dir, _push,
            app.app_type or "unknown", app.start_command or "", _image_port(app),
            **_image_build_kwargs(app),
        )
        app.docker_image = containers_naming.image_name(app_id, app.name)
        built_revision = await asyncio.to_thread(_refresh_app_source_revision, app)
        if built_revision:
            app.image_revision = built_revision

    ext_port = replica.external_port or app.port or 8000
    await _set_replica_substatus(replica.id, "creating_container")
    if desired_revision and app.image_revision != desired_revision:
        await _build_image()
    try:
        return await _run_replica(ext_port)
    except Exception as first_exc:
        msg = str(first_exc)
        bind_conflict = (
            "bind for " in msg.lower()
            or "port is already allocated" in msg.lower()
            or "failed to set up container networking" in msg.lower()
        )
        if bind_conflict:
            # Port taken — grab a free one via socket scan and retry (no build needed)
            import socket as _sock
            new_port = ext_port + 1
            while True:
                with _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM) as _s:
                    if _s.connect_ex(('127.0.0.1', new_port)) != 0:
                        break
                new_port += 1
            replica.external_port = new_port
            pm._push_line(app_id, f"[Docker] Port conflict on :{ext_port}; retrying replica on :{new_port}")
            try:
                return await _run_replica(new_port)
            except Exception as retry_exc:
                raise HTTPException(500, f"Failed to start instance: {retry_exc}") from retry_exc
        # Not a port conflict — image may not exist yet (first start after deploy)
        await _build_image()
        try:
            return await _run_replica(ext_port)
        except Exception as build_retry_exc:
            msg2 = str(build_retry_exc)
            bind2 = (
                "bind for " in msg2.lower()
                or "port is already allocated" in msg2.lower()
                or "failed to set up container networking" in msg2.lower()
            )
            if not bind2:
                raise HTTPException(500, f"Failed to start instance: {build_retry_exc}") from build_retry_exc
            import socket as _sock2
            new_port2 = ext_port + 1
            while True:
                with _sock2.socket(_sock2.AF_INET, _sock2.SOCK_STREAM) as s:
                    if s.connect_ex(('127.0.0.1', new_port2)) != 0:
                        break
                new_port2 += 1
            replica.external_port = new_port2
            pm._push_line(app_id, f"[Docker] Port conflict after build on :{ext_port}; retrying on :{new_port2}")
            return await _run_replica(new_port2)


def _derive_app_status_from_instances(replicas: list) -> str:
    """Derive the overall app status from its instance statuses."""
    statuses = {r.status for r in replicas}
    if "running" in statuses:
        return "running"
    if "starting" in statuses:
        return "starting"
    if "restarting" in statuses:
        return "restarting"
    if "stopping" in statuses:
        return "stopping"
    if "deploying" in statuses:
        return "deploying"
    if statuses and statuses - {"stopped", "error", "pending"} == set():
        if statuses == {"error"} or (statuses - {"stopped", "pending"} == {"error"}):
            return "error"
    return "stopped"


async def _start_replica_runtime(
    app: Application,
    replica: ApplicationReplica,
    replica_node: Optional[Node],
    env_vars: dict,
    db: AsyncSession,
) -> None:
    if replica_node is None:
        replica.status = "error"
        replica.last_error = "Replica node not found"
        return

    if replica_node.is_local:
        await _set_replica_substatus(replica.id, "creating_container")
        cid = await _start_instance_local(app, replica, env_vars, app.id)
        await _set_replica_substatus(replica.id, "waiting")
        replica.status = "running"
        replica.substatus = None
        replica.container_id = cid
        replica.last_error = None
        return

    if replica_node.status != "online" or not replica_node.enabled:
        replica.status = "error"
        replica.last_error = f"Node '{replica_node.name}' is offline"
        return

    remote_payload = _remote_replica_command_payload(
        app,
        env_vars,
        replica.external_port or app.port or 8000,
    )
    await queue_node_command(
        db,
        node_id=replica_node.id,
        app_id=app.id,
        command_type="start_replica",
        payload={**remote_payload, "replica_id": replica.id},
    )
    replica.status = "starting"
    replica.last_error = None


async def _stop_replica_runtime(
    app: Application,
    replica: ApplicationReplica,
    replica_node: Optional[Node],
    db: AsyncSession,
) -> None:
    if replica_node is None or replica_node.is_local:
        await asyncio.to_thread(pm.stop_docker_replica, app.id, replica.id)
        replica.status = "stopped"
        replica.container_id = None
        replica.tunnel_port = None
        replica.last_error = None
        return

    if replica_node.status == "online" and replica_node.enabled:
        await queue_node_command(
            db,
            node_id=replica_node.id,
            app_id=app.id,
            command_type="stop_replica",
            payload={"app_id": app.id, "replica_id": replica.id, "app_name": app.name},
        )
        replica.status = "stopping"
        replica.last_error = None
        return

    replica.status = "stopped"
    replica.container_id = None
    replica.tunnel_port = None
    replica.last_error = None


def _has_live_replicas(replicas: list[ApplicationReplica]) -> bool:
    return any(r.status in ("running", "starting", "restarting") for r in replicas)


async def _get_app_node(app: Application, db: AsyncSession, local_node: Optional[Node] = None) -> Node:
    """Return the node that should handle single-node operations (logs, git, rebuild).

    Prefers the node of the first running replica, then any replica, then local.
    """
    rep_result = await db.execute(
        select(ApplicationReplica).where(ApplicationReplica.app_id == app.id)
        .order_by(ApplicationReplica.status.desc())  # running > stopped
        .limit(5)
    )
    replicas = rep_result.scalars().all()
    running = next((r for r in replicas if r.status in ("running", "starting") and r.node_id), None)
    candidate = running or next((r for r in replicas if r.node_id), None)
    if candidate and candidate.node_id:
        node_result = await db.execute(select(Node).where(Node.id == candidate.node_id))
        node = node_result.scalar_one_or_none()
        if node:
            return node
    if local_node is not None:
        return local_node
    return await ensure_local_node(db)


async def _load_node_map(db: AsyncSession) -> dict[int, Node]:
    result = await db.execute(select(Node))
    nodes = result.scalars().all()
    return {n.id: n for n in nodes}
