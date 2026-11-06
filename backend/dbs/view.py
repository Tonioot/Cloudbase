from typing import Optional
from fastapi import Request
from dbs import engines as db_engines, manager as db_manager, proxy as db_proxy
from core.models import Node, Database
from dbs.ops import _allowlist, _node_reachable
from dbs.relay import _public_host
from dbs.state import _busy, _operation, _remote_progress, _update_info

def _safe_public_port(database_id: int) -> Optional[int]:
    try:
        return db_proxy.public_port(database_id)
    except RuntimeError:
        return None


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
        return db_manager.progress(database.id)
    return _remote_progress.get(database.node_id, {}).get(str(database.id))


def _as_dict(database: Database, node: Optional[Node], link_count: int = 0) -> dict:
    engine = db_engines.ENGINES.get(database.engine)
    return {
        "id": database.id,
        "name": database.name,
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
        "host": db_manager.container_name(database.id),
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
        "public_port": _safe_public_port(database.id),
        "public_host": _public_host(),
        "public_listening": db_proxy.public_listening(database.id),
        "public_tls": bool(database.public_tls),
        "link_count": link_count,
        "created_at": database.created_at.isoformat() if database.created_at else None,
    }
