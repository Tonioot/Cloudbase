from typing import Optional
from fastapi import Request
from dbs import engines as db_engines, proxy as db_proxy
from dbs import docker as dbs_docker
from core.models import Node, Database
from core import system_config as _syscfg
from dbs.ops import _allowlist, _node_reachable
from dbs import public_certs
from dbs.public import _public_host, public_host_for
from dbs.state import _busy, _operation, _remote_progress, _update_info

def _client_ip(request: Request) -> str:
    """The browser's public IP, also behind the local nginx or Cloudflare. Only used
    to prefill the allowlist, never to grant access, so forwarded headers are fine."""
    host = request.client.host if request.client else ""
    if host in ("127.0.0.1", "::1"):
        forwarded = (
            request.headers.get("cf-connecting-ip")
            or (request.headers.get("x-forwarded-for") or "").split(",")[0]
            or request.headers.get("x-real-ip")
            or ""
        ).strip()
        host = forwarded or host
    return host


def _progress(database: Database, node: Optional[Node]) -> Optional[str]:
    """What the database is doing before its container exists (e.g. pulling the image)."""
    if node is not None and node.is_local:
        return dbs_docker.progress(database.id)
    return _remote_progress.get(database.node_id, {}).get(str(database.id))


def _as_dict(database: Database, node: Optional[Node], link_count: int = 0) -> dict:
    engine = db_engines.ENGINES.get(database.engine)
    return {
        "id": database.id,
        "name": database.name,
        "project_id": database.project_id,
        "engine": database.engine,
        "engine_label": engine.label if engine else database.engine,
        "version": database.version,
        "node_id": database.node_id,
        "node_name": node.name if node else None,
        "node_online": _node_reachable(node) if node else False,
        "status": database.status,
        "desired_state": database.desired_state,
        "last_error": database.last_error,
        "cpu_limit": database.cpu_limit,
        "memory_limit_mb": database.memory_limit_mb,
        "host": dbs_docker.container_name(database.id),
        "port": engine.port if engine else None,
        "default_env_var": engine.env_var if engine else "DATABASE_URL",
        "initialized": database.initialized_at is not None,
        "busy": database.id in _busy,
        "operation": _operation.get(database.id),
        "running_version": (_update_info.get(database.id) or {}).get("version"),
        "update": _update_info.get(database.id),
        "progress": _progress(database, node),
        "backup_schedule": database.backup_schedule,
        "can_restore": bool(engine and engine.can_restore),
        "public_enabled": bool(database.public_enabled),
        "public_allowlist": _allowlist(database),
        "public_port": database.public_port,
        "public_host": public_host_for(database),
        "public_ip": _public_host(),
        "public_hostname": database.public_hostname or "",
        "db_domain": _syscfg.get_db_domain_cached(),
        "public_trusted": public_certs.is_trusted(database.id),
        "public_cert_state": public_certs.STATE.get(database.id),
        "public_listening": db_proxy.public_listening(database.id),
        "public_tls": bool(database.public_tls),
        "link_count": link_count,
        "created_at": database.created_at.isoformat() if database.created_at else None,
    }
