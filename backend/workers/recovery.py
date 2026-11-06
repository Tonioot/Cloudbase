import asyncio
import logging
import os
from sqlalchemy import select
from core.database import AsyncSessionLocal
from core.models import Application, Node as _Node, ApplicationReplica
from nodes import registry as nodes_registry
from apps import runtime as apps_runtime
from core.env_crypto import decrypt_env
from nginx import manager as nm
from containers import processes as pm
from containers import replicas as containers_replicas
from containers import stats_logs as containers_stats_logs
from containers import naming as containers_naming
from core import system_config as _syscfg
from apps.runtime import _derive_app_status_from_instances
from apps.nginx_sync import _write_app_nginx_config as _startup_nginx
from nodes.registry import ensure_local_node as _startup_local_node
from nginx.panel import _refresh_panel_nginx

log = logging.getLogger("cloudbase.main")


def _restore_stuck_restart_configs(apps: list[Application]) -> None:
    for app in apps:
        proxy_port = app.external_port or app.port
        has_custom = bool(app.nginx_enabled and app.domain)
        has_auto = bool(_syscfg.get_base_domain_cached())
        if not ((has_custom or has_auto) and proxy_port):
            continue
        if app.maintenance_mode or app.update_mode:
            continue

        config_path = nm.get_config_path(app.name)
        try:
            if not os.path.exists(config_path):
                continue
            with open(config_path, encoding="utf-8") as f:
                current_config = f.read()
        except Exception:
            continue

        if not nm.config_uses_restart_page(current_config):
            continue

        log.debug(
            f"STARTUP nginx recovery for app {app.id} ({app.name}): "
            "restart page config detected, restoring normal proxy"
        )
        normal_cfg = nm.generate_config(
            app.name,
            app.domain if has_custom else None,
            proxy_port,
            app.ssl_cert_path if has_custom else None,
            app.ssl_key_path if has_custom else None,
            app_id=app.id,
            mode="normal",
        )
        ok, msg = nm.write_nginx_config(app.name, normal_cfg)
        log.debug(f"STARTUP nginx recovery result for app {app.id} ({app.name}): ok={ok} msg={msg!r}")
        if ok:
            pm._push_line(app.id, "Recovered a stale restart page after Cloudbase startup.")


def _docker_runtime_options(app: Application) -> dict:
    return apps_runtime._docker_runtime_options(app)


async def _startup_recovery() -> None:
    """Bring local instances, nginx configs and the panel config in line with the DB."""
    # Recover running instances and auto-start
    async with AsyncSessionLocal() as db:
        await nodes_registry.ensure_local_node(db)

        # Recover replica container statuses
        rep_result = await db.execute(select(ApplicationReplica))
        all_replicas = rep_result.scalars().all()
        node_map: dict = {}
        for replica in all_replicas:
            r_node_id = replica.node_id
            if r_node_id not in node_map:
                nr = await db.execute(select(_Node).where(_Node.id == r_node_id))
                node_map[r_node_id] = nr.scalar_one_or_none()
            r_node = node_map.get(r_node_id)
            if r_node and not r_node.is_local:
                continue  # remote replicas managed by their own agent
            if replica.status in ("starting", "stopping", "deploying"):
                continue
            alive = containers_replicas.is_replica_container_running(replica.app_id, replica.id)
            new_status = "running" if alive else "stopped"
            if new_status != replica.status:
                replica.status = new_status
                log.debug(f"RECOVERY replica {replica.id} app={replica.app_id}: → {new_status}")
            if alive:
                # Re-attach to the replica's own container (not the legacy cloudbase-app-{id}
                # name), otherwise live logs stay silent after a Cloudbase restart.
                containers_stats_logs.attach_container_log_tailer(
                    replica.app_id, pm.log_buffers, pm._push_container_line, asyncio.get_event_loop(),
                    cname=containers_naming.replica_container_name(replica.app_id, replica.id),
                )

        # Kill any orphan legacy containers (cloudbase-app-{id} without replica row)
        app_ids_with_replicas = {r.app_id for r in all_replicas}
        for app_id in list(app_ids_with_replicas):
            if pm.is_docker_app_running(app_id):
                pm.stop_docker_app(app_id)
                log.debug(f"RECOVERY killed orphan legacy container for app {app_id}")

        # Sync app statuses from their replicas
        result = await db.execute(select(Application))
        apps = result.scalars().all()
        replica_map: dict[int, list] = {}
        for r in all_replicas:
            replica_map.setdefault(r.app_id, []).append(r)

        for a in apps:
            app_replicas = replica_map.get(a.id)
            if app_replicas:
                a.status = _derive_app_status_from_instances(app_replicas)
            # auto_start: start all stopped local replicas
            if a.auto_start and a.start_command and a.working_dir and app_replicas:
                env_vars = decrypt_env(a.env_vars or "")
                for replica in app_replicas:
                    r_node = node_map.get(replica.node_id)
                    if r_node and not r_node.is_local:
                        continue
                    if replica.status != "stopped":
                        continue
                    try:
                        cid = pm.start_docker_replica(
                            a.id, replica.id, a.name,
                            a.port or 8000, replica.external_port or a.port or 8000,
                            env_vars, _docker_runtime_options(a),
                        )
                        replica.status = "running"
                        replica.container_id = cid
                        log.debug(f"AUTO-START replica {replica.id} app={a.id}: {cid[:12]}")
                    except Exception as exc:
                        log.debug(f"AUTO-START replica {replica.id} app={a.id}: FAILED — {exc}")
                a.status = _derive_app_status_from_instances(app_replicas)

        await db.commit()
        await asyncio.to_thread(_restore_stuck_restart_configs, apps)

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
