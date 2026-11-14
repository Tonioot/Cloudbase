from apps import health
import json
from typing import Optional
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from core.models import Application, Deployment, Node, Instance
from core import activity
from nginx import certbot as nginx_certbot
from cron import scheduler as _cron
from core.env_crypto import decrypt_env
from core import system_config as _syscfg
from apps.git import _github_token_label
from apps.runtime import status_of_instances

async def _get_or_404(app_id: int, db: AsyncSession) -> Application:
    result = await db.execute(select(Application).where(Application.id == app_id))
    app = result.scalar_one_or_none()
    if not app:
        raise HTTPException(404, "App not found")
    return app


def app_status(app: Application, instance_statuses) -> str:
    """What the app is: it follows from its instances (a cron job from its schedule); nothing stores it."""
    if app.cron_schedule:
        return _cron.status_of(app)
    if not instance_statuses:
        return "error" if app.last_error else "stopped"      # no instance: stopped, or the last deploy failed
    return status_of_instances(instance_statuses)


async def _instance_views(app_id: int, db: AsyncSession) -> list[dict]:
    rows = (await db.execute(
        select(Instance, Node).join(Node, Instance.node_id == Node.id, isouter=True)
        .where(Instance.app_id == app_id).order_by(Instance.id)
    )).all()
    return [_instance_to_dict(instance, node) for instance, node in rows]


def public_url(app: Application) -> Optional[str]:
    """Where visitors reach the app: its custom domain first, then its automatic subdomain; None without one."""
    import re as _re
    if app.no_web:
        return None
    if app.domain:
        return f"{'https' if (app.ssl_cert_path and app.ssl_key_path) else 'http'}://{app.domain}"
    _base = _syscfg.get_base_domain_cached()
    if not _base:
        return None
    _slug = _re.sub(r"[^a-z0-9]+", "-", (app.name or "").lower()).strip("-")
    if not _slug:
        return None
    _auto = f"{_slug}.{_base}"
    _secure = bool(_syscfg.get_base_ssl_cert_cached()) or (nginx_certbot.capability()[0] and nginx_certbot.exists(_auto))
    return f"{'https' if _secure else 'http'}://{_auto}"


def _app_to_dict(
    app: Application,
    instances: list,
    include_sensitive: bool = True,
    include_page_configs: bool = True,
) -> dict:
    downtime_page = json.loads(app.downtime_page or "{}") if include_page_configs else None
    update_page = json.loads(app.update_page or "{}") if include_page_configs else None
    restart_page = json.loads(app.restart_page or "{}") if include_page_configs else None
    starting_page = json.loads(app.starting_page or "{}") if include_page_configs else None
    _app_url = public_url(app)
    return {
        "id": app.id,
        "name": app.name,
        "repo_url": app.repo_url,
        "domain": app.domain,
        "extra_domains": json.loads(app.extra_domains or "[]"),
        "redirect_domains": json.loads(app.redirect_domains or "[]"),
        "no_web": bool(app.no_web),
        "app_type": app.app_type,
        "start_command": app.start_command,
        "build_command": app.build_command,
        "port": app.port,
        "status": app_status(app, [r["status"] for r in instances]),
        "activity": activity.get(app.id),        # what is being done to the app right now, or None
        "working_dir": app.working_dir,
        "last_error": app.last_error,
        "env_vars": {k: "" for k in decrypt_env(app.env_vars or "")} if include_sensitive else {},
        "nginx_enabled": app.nginx_enabled,
        "restart_policy": app.restart_policy or "no",
        "docker_image":   app.docker_image,
        "source_revision": app.source_revision,
        "image_revision": app.image_revision,
        "docker_cpu_limit": app.docker_cpu_limit,
        "docker_memory_limit_mb": app.docker_memory_limit_mb,
        "docker_read_only_root": bool(app.docker_read_only_root),
        "block_local_network": bool(app.block_local_network),
        "docker_tmpfs_enabled": bool(app.docker_tmpfs_enabled),
        "docker_tmpfs_size_mb": app.docker_tmpfs_size_mb,
        "autoscale_enabled":      bool(app.autoscale_enabled),
        "autoscale_min_instances": app.autoscale_min_instances or 1,
        "autoscale_max_instances": app.autoscale_max_instances or 4,
        "autoscale_cpu_target":   app.autoscale_cpu_target or 70.0,
        "autoscale_memory_target_mb": app.autoscale_memory_target_mb,
        "autoscale_up_seconds":   app.autoscale_up_seconds or 60,
        "autoscale_down_minutes": app.autoscale_down_minutes or 5,
        "autoscale_max_step_up":  app.autoscale_max_step_up,
        "autoscale_max_step_down": app.autoscale_max_step_down,
        "autoscale_cooldown_seconds": app.autoscale_cooldown_seconds,
        "maintenance_mode": app.maintenance_mode or False,
        "update_mode":      app.update_mode or False,
        "downtime_page":    downtime_page,
        "update_page":      update_page,
        "restart_page":     restart_page,
        "starting_page":    starting_page,
        "ssl_cert_path": app.ssl_cert_path,
        "ssl_key_path": app.ssl_key_path,
        "github_token_set": bool(app.github_token),
        "github_token_id": app.github_token_id or None,
        "github_token_label": _github_token_label(app),
        "created_at": app.created_at.isoformat() if app.created_at else None,
        "updated_at": app.updated_at.isoformat() if app.updated_at else None,
        "app_url": _app_url,
        "auto_deploy": bool(app.auto_deploy),
        "deploy_branch": app.deploy_branch,
        "deploy_strategy": app.deploy_strategy or "rolling",
        "auto_deploy_interval": app.auto_deploy_interval or 60,
        "health_path": health.settings_of(app)[0],
        "health_expect": health.settings_of(app)[1],
        "health_timeout_seconds": health.settings_of(app)[2],
        "show_on_status_page": bool(app.show_on_status_page),
        "uptime_path": app.uptime_path or "",
        "uptime_expect": app.uptime_expect or "",
        "uptime_interval_seconds": app.uptime_interval_seconds if app.uptime_interval_seconds is not None else 60,
        "uptime_failures": app.uptime_failures or 2,
        "instances": instances,
        "instance_count": len(instances),
        "node_id": app.node_id,
        "cron": _cron.info(app),
    }


def _instance_to_dict(instance: Instance, node: Optional[Node] = None) -> dict:
    return {
        "id": instance.id,
        "app_id": instance.app_id,
        "node_id": instance.node_id,
        "node_name": node.name if node else None,
        "node_is_local": bool(node.is_local) if node else False,
        "external_port": instance.external_port,
        "wg_state": node.wg_state if node and not node.is_local else None,
        "container_id": instance.container_id,
        "status": instance.status,
        "substatus": instance.substatus,
        "last_error": instance.last_error,
        "docker_cpu_limit": instance.docker_cpu_limit,
        "docker_memory_limit_mb": instance.docker_memory_limit_mb,
        "docker_read_only_root": bool(instance.docker_read_only_root),
        "docker_tmpfs_enabled": bool(instance.docker_tmpfs_enabled),
        "docker_tmpfs_size_mb": instance.docker_tmpfs_size_mb,
        "created_at": instance.created_at.isoformat() if instance.created_at else None,
        "updated_at": instance.updated_at.isoformat() if instance.updated_at else None,
    }


# ── Pull & deploy (one streaming action) ──────────────────────────────────────

def _deployment_dict(d: Deployment, with_log: bool = False) -> dict:
    out = {
        "id": d.id,
        "commit_sha": d.commit_sha,
        "commit_message": d.commit_message,
        "commit_author": d.commit_author,
        "branch": d.branch,
        "trigger": d.trigger,
        "strategy": d.strategy,
        "status": d.status,
        "error": d.error,
        "actor": d.actor,
        "created_at": d.created_at.isoformat() + "Z" if d.created_at else None,
        "finished_at": d.finished_at.isoformat() + "Z" if d.finished_at else None,
    }
    if with_log:
        out["log"] = d.log or ""
    return out
