import json
from typing import Optional
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from core.models import Application, Deployment, Node, ApplicationReplica
from nginx import certificates as certs
from cron import scheduler as _cron
from core.env_crypto import decrypt_env
from core import system_config as _syscfg
from apps.git import _github_token_label

async def _get_or_404(app_id: int, db: AsyncSession) -> Application:
    result = await db.execute(select(Application).where(Application.id == app_id))
    app = result.scalar_one_or_none()
    if not app:
        raise HTTPException(404, "App not found")
    return app


def _app_to_dict(
    app: Application,
    include_sensitive: bool = True,
    replicas: Optional[list] = None,
    include_page_configs: bool = True,
) -> dict:
    import re as _re
    downtime_page = json.loads(app.downtime_page or "{}") if include_page_configs else None
    update_page = json.loads(app.update_page or "{}") if include_page_configs else None
    restart_page = json.loads(app.restart_page or "{}") if include_page_configs else None
    starting_page = json.loads(app.starting_page or "{}") if include_page_configs else None
    # Compute the app's accessible URL: custom domain first, then auto-subdomain
    _app_url: Optional[str] = None
    if not app.no_web:
        if app.domain:
            _proto = "https" if (app.ssl_cert_path and app.ssl_key_path) else "http"
            _app_url = f"{_proto}://{app.domain}"
        else:
            _base = _syscfg.get_base_domain_cached()
            if _base:
                _slug = _re.sub(r"[^a-z0-9]+", "-", (app.name or "").lower()).strip("-")
                if _slug:
                    _auto = f"{_slug}.{_base}"
                    _secure = bool(_syscfg.get_base_ssl_cert_cached()) or (certs.capability()[0] and certs.exists(_auto))
                    _app_url = f"{'https' if _secure else 'http'}://{_auto}"
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
        "status": app.status,
        "working_dir": app.working_dir,
        "last_error": app.last_error,
        "env_vars": {k: "" for k in decrypt_env(app.env_vars or "")} if include_sensitive else {},
        "nginx_enabled": app.nginx_enabled,
        "auto_start":     app.auto_start,
        "restart_policy": app.restart_policy or "no",
        "docker_image":   app.docker_image,
        "source_revision": app.source_revision,
        "image_revision": app.image_revision,
        "docker_cpu_limit": app.docker_cpu_limit,
        "docker_memory_limit_mb": app.docker_memory_limit_mb,
        "docker_read_only_root": bool(app.docker_read_only_root),
        "docker_tmpfs_enabled": bool(app.docker_tmpfs_enabled),
        "docker_tmpfs_size_mb": app.docker_tmpfs_size_mb,
        "autoscale_enabled":      bool(app.autoscale_enabled),
        "autoscale_min_replicas": app.autoscale_min_replicas or 1,
        "autoscale_max_replicas": app.autoscale_max_replicas or 4,
        "autoscale_cpu_target":   app.autoscale_cpu_target or 70.0,
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
        "auto_deploy": bool(getattr(app, "auto_deploy", False)),
        "deploy_branch": getattr(app, "deploy_branch", None),
        "deploy_strategy": getattr(app, "deploy_strategy", None) or "rolling",
        "auto_deploy_interval": getattr(app, "auto_deploy_interval", None) or 60,
        "replicas": replicas if replicas is not None else [],
        "replica_count": len(replicas) if replicas is not None else 0,
        "node_id": app.node_id,
        "cron": _cron.info(app),
    }


def _replica_to_dict(replica: ApplicationReplica, node: Optional[Node] = None) -> dict:
    return {
        "id": replica.id,
        "app_id": replica.app_id,
        "node_id": replica.node_id,
        "node_name": node.name if node else None,
        "node_is_local": bool(node.is_local) if node else False,
        "external_port": replica.external_port,
        "tunnel_port": replica.tunnel_port,
        "tunnel_connected": replica.tunnel_port is not None,
        "container_id": replica.container_id,
        "status": replica.status,
        "substatus": replica.substatus,
        "last_error": replica.last_error,
        "docker_cpu_limit": replica.docker_cpu_limit,
        "docker_memory_limit_mb": replica.docker_memory_limit_mb,
        "docker_read_only_root": bool(replica.docker_read_only_root),
        "docker_tmpfs_enabled": bool(replica.docker_tmpfs_enabled),
        "docker_tmpfs_size_mb": replica.docker_tmpfs_size_mb,
        "created_at": replica.created_at.isoformat() if replica.created_at else None,
        "updated_at": replica.updated_at.isoformat() if replica.updated_at else None,
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
