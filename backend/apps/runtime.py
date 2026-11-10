import asyncio
from typing import Optional
from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession
from core.models import Deployment, StatsHistory, ApplicationReplica, Node, Application, AuditLog
from containers import processes as pm
from core import applog
from dbs import links as db_links
from nodes.registry import ensure_local_node
from nodes import desired
from apps.git import _decrypt_github_token, _refresh_app_source_revision
from apps.images import _image_port
from apps.nginx_sync import _has_public_nginx_domain

async def _cleanup_app_dependencies(db: AsyncSession, app_id: int) -> None:
    """Remove or detach rows that can still reference an app in older DB schemas."""
    # App ids can be reused; a new app must not inherit this one's Cloudbase log.
    applog.forget(app_id)
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
        # Reached through nginx on localhost: then the host port must not be open to the
        # internet as well. Without a domain the host port is the only way in, so it stays
        # public. (On another node the agent binds it to its node-network address instead.)
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


def status_of_instances(statuses) -> str:
    """The status of a (non-cron) app from the statuses of its instances."""
    statuses = set(statuses)
    for status in ("running", "starting", "stopping"):
        if status in statuses:
            return status
    if "error" in statuses and statuses <= {"stopped", "error", "pending"}:
        return "error"
    return "stopped"


def has_running_instance():
    """SQL condition for 'this app runs': at least one instance is running."""
    return Application.id.in_(select(ApplicationReplica.app_id).where(ApplicationReplica.status == "running"))


def _instance_payload(app: Application, replica: ApplicationReplica, env_vars: dict,
                      source_revision: Optional[str] = None) -> dict:
    """The spec of one instance: the app's settings, with the instance's own limits on top."""
    payload = _remote_replica_command_payload(app, env_vars, replica.external_port or app.port or 8000, source_revision)
    for key in ("docker_cpu_limit", "docker_memory_limit_mb", "docker_tmpfs_size_mb"):
        if getattr(replica, key) is not None:
            payload[key] = getattr(replica, key)
    for key in ("docker_read_only_root", "docker_tmpfs_enabled"):
        if getattr(replica, key):
            payload[key] = True
    return payload


async def _start_replica_runtime(
    app: Application,
    replica: ApplicationReplica,
    replica_node: Optional[Node],
    env_vars: dict,
    db: AsyncSession,
    source_revision: Optional[str] = None,
) -> None:
    """Start (or, for a running one, restart) an instance: write its spec on the list of its node.

    The node's reconciler does the rest, and that includes this server's own (nodes/local.py). A new spec makes
    it recreate the container, which is what a restart is."""
    if replica_node is None:
        replica.status = "error"
        replica.last_error = "Replica node not found"
        return
    if not replica_node.is_local and (replica_node.status != "online" or not replica_node.enabled):
        replica.status = "error"
        replica.last_error = f"Node '{replica_node.name}' is offline"
        return
    if source_revision is None:
        source_revision = await asyncio.to_thread(_refresh_app_source_revision, app)
    await desired.start(db, node_id=replica_node.id, replica_id=replica.id,
                        payload=_instance_payload(app, replica, env_vars, source_revision))
    replica.status = "starting"
    replica.substatus = None
    replica.last_error = None


async def _stop_replica_runtime(
    app: Application,
    replica: ApplicationReplica,
    replica_node: Optional[Node],
    db: AsyncSession,
) -> None:
    """Stop an instance: it should not run any more; its row stays so it can be started again."""
    if replica_node is None:
        replica.status = "stopped"
        replica.container_id = None
        replica.last_error = None
        return

    await desired.stop(db, node_id=replica_node.id, replica_id=replica.id)
    if replica_node.is_local or (replica_node.status == "online" and replica_node.enabled):
        replica.status = "stopping"                 # "stopped" once the node reports the container gone
        replica.last_error = None
        return

    replica.status = "stopped"
    replica.container_id = None
    replica.last_error = None


def _has_live_replicas(replicas: list[ApplicationReplica]) -> bool:
    return any(r.status in ("running", "starting") for r in replicas)


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
    candidate = next((r for r in replicas if r.status in ("running", "starting")), None) or next(iter(replicas), None)
    if candidate:
        node = await db.get(Node, candidate.node_id)
        if node:
            return node
    if local_node is not None:
        return local_node
    return await ensure_local_node(db)


async def _load_node_map(db: AsyncSession) -> dict[int, Node]:
    result = await db.execute(select(Node))
    nodes = result.scalars().all()
    return {n.id: n for n in nodes}
