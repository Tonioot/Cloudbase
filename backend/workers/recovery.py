import asyncio
import logging
from sqlalchemy import select
from core.database import AsyncSessionLocal
from core.models import Application, ApplicationReplica
from nodes import registry as nodes_registry
from apps import runtime as apps_runtime
from core.env_crypto import decrypt_env
from containers import processes as pm
from containers import replicas as containers_replicas
from containers import stats_logs as containers_stats_logs
from core import system_config as _syscfg
from apps.runtime import _derive_app_status_from_instances
from apps.nginx_sync import _write_app_nginx_config as _startup_nginx
from nodes.registry import ensure_local_node as _startup_local_node
from nginx.panel import _refresh_panel_nginx

log = logging.getLogger("cloudbase.main")


async def _startup_recovery() -> None:
    """Bring the log tailers, auto-start apps, nginx configs and the panel config in line with the DB.

    Keeping this server's instances running (and repairing them) is the local reconciler's job
    (nodes/local.py); here is only what has to happen once, as the panel starts."""
    async with AsyncSessionLocal() as db:
        local_node = await nodes_registry.ensure_local_node(db)

    # Live logs of the containers that kept running while the panel was down
    try:
        for container in await asyncio.to_thread(containers_replicas.list_replica_containers):
            if container["status"] == "running":
                containers_stats_logs.attach_container_log_tailer(
                    container["app_id"], pm.log_buffers, pm._push_container_line, asyncio.get_event_loop(),
                    cname=container["name"],
                )
    except Exception as exc:
        log.warning("STARTUP could not attach the live logs: %s", exc)

    async with AsyncSessionLocal() as db:
        replicas = (await db.execute(select(ApplicationReplica))).scalars().all()
        replica_map: dict[int, list] = {}
        for r in replicas:
            replica_map.setdefault(r.app_id, []).append(r)

        for a in (await db.execute(select(Application))).scalars().all():
            app_replicas = replica_map.get(a.id)
            if not app_replicas:
                continue
            # auto_start: start the stopped instances that run on this server
            if a.auto_start and a.start_command and a.working_dir:
                env_vars = decrypt_env(a.env_vars or "")
                for replica in app_replicas:
                    if replica.node_id == local_node.id and replica.status == "stopped":
                        await apps_runtime._start_replica_runtime(a, replica, local_node, env_vars, db)
            a.status = _derive_app_status_from_instances(app_replicas)
        await db.commit()

    # Regenerate nginx configs from live instance state so stale configs on disk
    # (written before the instance-based model) are replaced with correct upstreams.
    async with AsyncSessionLocal() as db:
        _startup_base = _syscfg.get_base_domain_cached()
        result = await db.execute(select(Application))
        _startup_local = await _startup_local_node(db)
        for a in result.scalars().all():
            has_custom = bool(a.nginx_enabled and a.domain)
            if not has_custom and not _startup_base:
                continue
            try:
                await _startup_nginx(a, db, _startup_local)
                log.debug(f"STARTUP nginx regenerated for app {a.id} ({a.name})")
            except Exception as exc:
                log.debug(f"STARTUP nginx regen failed for app {a.id}: {exc}")

    # The panel's own nginx config is otherwise only written on "Save & apply";
    # rewrite it so it always matches the current generator (e.g. serves the
    # Let's Encrypt verification folder).
    try:
        await asyncio.to_thread(_refresh_panel_nginx)
    except Exception as exc:
        log.warning("STARTUP panel nginx refresh failed: %s", exc)
