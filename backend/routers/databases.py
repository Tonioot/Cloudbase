"""Managed databases: one container with its own data volume on one fixed node.

The primary owns the state. Operations run in the background: locally through
db_manager, on remote nodes through the agent's db_* commands. A monitor loop keeps
the status in sync with Docker and restarts databases that should be running.
"""

import asyncio
import json
import logging
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, WebSocket
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import auth as _auth
import db_engines
import db_links
import db_manager
import db_proxy
from audit import log_audit
from database import get_db, AsyncSessionLocal
from env_crypto import encrypt_text, decrypt_env
from models import Application, ApplicationReplica, Database, DatabaseLink, DatabaseStatsHistory, Node, SystemConfig
from routers.nodes import queue_node_command, wait_for_node_command, get_node_by_token_or_401

router = APIRouter(prefix="/api/databases", tags=["databases"])
# Node-facing WebSockets for database connections across nodes.
relay_router = APIRouter(prefix="/api/nodes/ws", tags=["databases"])
log = logging.getLogger("cloudbase.databases")

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")
ENV_VAR_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
MIN_MEMORY_MB = {"redis": 32, "valkey": 32, "postgres": 128}
DEFAULT_MIN_MEMORY_MB = 256

# Pulling an image on a slow node can take minutes.
PROVISION_TIMEOUT_SECONDS = 900
COMMAND_TIMEOUT_SECONDS = 120
MONITOR_INTERVAL_SECONDS = 30
HEAL_BACKOFF_SECONDS = 120

DIAL_TIMEOUT_SECONDS = 10

# Databases with an operation in flight; the monitor leaves them alone.
_busy: set[int] = set()
_operation: dict[int, str] = {}  # what a busy database is doing, e.g. "Backing up…"
# Result of the last update check per database: {version, update_available, latest_version, error, checked_at}
_update_info: dict[int, dict] = {}
_last_heal: dict[int, float] = {}

# Cross-node connections: control channel per node, dials waiting for their node.
_control: dict[int, tuple[WebSocket, asyncio.Lock]] = {}
_pending_dials: dict[str, tuple[asyncio.Future, int]] = {}
_desired_proxies: dict[int, set[int]] = {}  # node id -> database ids it proxies
_remote_progress: dict[int, dict[str, str]] = {}  # node id -> {database id: progress}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _spec(database: Database) -> dict:
    return {
        "database_id": database.id,
        "name": database.name,
        "engine": database.engine,
        "version": database.version,
        "credentials": db_links.decrypt_credentials(database),
        "cpu_limit": database.cpu_limit,
        "memory_limit_mb": database.memory_limit_mb,
        "initialized": database.initialized_at is not None,
        "volume_token": _volume_token(database),
    }


def _volume_token(database: Database) -> str:
    """Identifies the data volume of exactly this database (see db_manager.ensure_running)."""
    created = database.created_at.isoformat() if database.created_at else ""
    return f"{database.id}:{created}"


async def _next_database_id(db: AsyncSession) -> int:
    """Database ids are never reused: they name the data volume and the ports, and a
    new database must not pick up the volume of a deleted one. SQLite reuses the
    highest id after a delete, so keep our own high-water mark."""
    row = (await db.execute(select(SystemConfig).where(SystemConfig.key == "databases.last_id"))).scalar_one_or_none()
    highest = (await db.execute(select(func.max(Database.id)))).scalar() or 0
    next_id = max(int(row.value or 0) if row else 0, highest) + 1
    if row is None:
        db.add(SystemConfig(key="databases.last_id", value=str(next_id)))
    else:
        row.value = str(next_id)
    return next_id


def _data_dir(database: Database) -> str:
    return db_engines.get_engine(database.engine).data_dir(database.version)


def _new_credentials(name: str, engine: str) -> dict:
    ident = name.replace("-", "_")
    return {
        "user": "default" if engine in ("redis", "valkey") else ident,
        "password": secrets.token_urlsafe(24),
        "root_password": secrets.token_urlsafe(24),
        "db_name": ident,
    }


async def _get_or_404(database_id: int, db: AsyncSession) -> Database:
    database = await db.get(Database, database_id)
    if database is None:
        raise HTTPException(404, "Database not found")
    return database


async def _node_for(database: Database, db: AsyncSession) -> Node:
    node = await db.get(Node, database.node_id)
    if node is None:
        raise HTTPException(409, "The node of this database no longer exists")
    return node


def _node_reachable(node: Node) -> bool:
    return node.is_local or (node.enabled and node.status == "online")


async def _run_on_node(node: Node, local_fn, command_type: str, payload: dict,
                       timeout: float = COMMAND_TIMEOUT_SECONDS) -> dict:
    """Run a db_manager call on the database's node and return its result."""
    if node.is_local:
        return await asyncio.to_thread(local_fn)
    if not _node_reachable(node):
        raise RuntimeError(f"Node '{node.name}' is offline")
    async with AsyncSessionLocal() as db:
        cmd = await queue_node_command(db, node_id=node.id, command_type=command_type, payload=payload)
        done = await wait_for_node_command(db, cmd.id, timeout_seconds=timeout)
    if done.status != "done":
        raise RuntimeError(done.error_message or f"{command_type} failed on node '{node.name}'")
    return json.loads(done.result or "{}")


def _state_to_status(state: str) -> tuple[str, Optional[str]]:
    if state == "restarting":
        return "error", "The database container keeps restarting. Check the logs."
    return {"running": "running", "starting": "starting", "unhealthy": "unhealthy"}.get(state, "stopped"), None


async def _set_status(database_id: int, status: str, error: Optional[str] = None, **fields) -> None:
    async with AsyncSessionLocal() as db:
        database = await db.get(Database, database_id)
        if database is None:
            return
        database.status = status
        database.last_error = error
        for key, value in fields.items():
            setattr(database, key, value)
        if status == "running" and database.initialized_at is None:
            database.initialized_at = datetime.utcnow()
        await db.commit()


def _spawn(database_id: int, coro_fn) -> None:
    """Run an operation in the background, one at a time per database."""
    _busy.add(database_id)

    async def runner():
        try:
            await coro_fn()
        except Exception as e:
            log.warning("[db] operation on database %s failed: %s", database_id, e)
            await _set_status(database_id, "error", str(e))
        finally:
            _busy.discard(database_id)

    asyncio.create_task(runner())


async def _start_operation(database_id: int, op: str) -> None:
    """op: ensure | recreate | restart"""
    async with AsyncSessionLocal() as db:
        database = await db.get(Database, database_id)
        if database is None:
            return
        node = await _node_for(database, db)
        spec = _spec(database)
    fn = {"ensure": db_manager.ensure_running, "recreate": db_manager.recreate, "restart": db_manager.restart}[op]
    timeout = COMMAND_TIMEOUT_SECONDS if op == "restart" else PROVISION_TIMEOUT_SECONDS
    result = await _run_on_node(node, lambda: fn(spec), f"db_{op}", {"spec": spec}, timeout=timeout)
    status, error = _state_to_status(result.get("state", "running"))
    await _set_status(database_id, status, error, container_id=result.get("container_id"))


def _require_idle(database: Database) -> None:
    if database.id in _busy:
        raise HTTPException(409, "Another operation on this database is still running")


async def _links_for(database_id: int, db: AsyncSession) -> list[dict]:
    result = await db.execute(
        select(DatabaseLink, Application)
        .join(Application, Application.id == DatabaseLink.app_id)
        .where(DatabaseLink.database_id == database_id)
        .order_by(Application.name)
    )
    rows = result.all()
    if not rows:
        return []
    app_ids = [app.id for _, app in rows]
    rep_result = await db.execute(
        select(ApplicationReplica.app_id, ApplicationReplica.node_id).where(ApplicationReplica.app_id.in_(app_ids))
    )
    nodes_by_app: dict[int, set] = {}
    for app_id, node_id in rep_result.all():
        nodes_by_app.setdefault(app_id, set()).add(node_id)
    database = await db.get(Database, database_id)
    links = []
    for link, app in rows:
        app_nodes = nodes_by_app.get(app.id) or ({app.node_id} if app.node_id else set())
        links.append({
            "id": link.id,
            "app_id": app.id,
            "app_name": app.name,
            "env_var_name": link.env_var_name,
            # Phase 1: only instances on the database's node can reach it.
            "instances_elsewhere": any(n is not None and n != database.node_id for n in app_nodes),
        })
    return links


def _allowlist(database: Database) -> list[str]:
    try:
        return json.loads(database.public_allowlist or "[]")
    except ValueError:
        return []


def _safe_public_port(database_id: int) -> Optional[int]:
    try:
        return db_proxy.public_port(database_id)
    except RuntimeError:
        return None


# The primary's public IP, refreshed by push_proxies(). Public database ports are
# always given as this IP: the panel domain may sit behind a proxy such as
# Cloudflare, which only carries web traffic, so database clients would time out.
_public_ip: Optional[str] = None


def _public_host() -> str:
    return _public_ip or ""


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


def _validate_limits(engine: str, cpu_limit: Optional[float], memory_limit_mb: Optional[int]) -> None:
    if cpu_limit is not None and not (0.1 <= cpu_limit <= 64):
        raise HTTPException(400, "CPU limit must be between 0.1 and 64 cores")
    if memory_limit_mb is not None:
        minimum = MIN_MEMORY_MB.get(engine, DEFAULT_MIN_MEMORY_MB)
        if memory_limit_mb < minimum:
            raise HTTPException(400, f"{db_engines.get_engine(engine).label} needs at least {minimum} MB of memory")


# ── Request models ────────────────────────────────────────────────────────────

class CreateDatabaseRequest(BaseModel):
    name: str
    engine: str
    version: Optional[str] = None
    node_id: Optional[int] = None
    cpu_limit: Optional[float] = None
    memory_limit_mb: Optional[int] = None


class UpdateDatabaseRequest(BaseModel):
    cpu_limit: Optional[float] = None
    memory_limit_mb: Optional[int] = None
    clear_cpu_limit: bool = False
    clear_memory_limit: bool = False


class PublicAccessRequest(BaseModel):
    enabled: bool
    allowlist: list[str] = Field(default_factory=list, max_length=100)
    tls: Optional[bool] = None  # None = keep the current setting


class LinkRequest(BaseModel):
    app_id: int
    env_var_name: Optional[str] = Field(default=None, max_length=100)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/engines")
async def list_engines(_user: dict = Depends(_auth.require_permission("databases.view"))):
    return db_engines.catalog()


@router.get("/tls-ca.crt")
async def tls_ca(_user: dict = Depends(_auth.require_permission("databases.view"))):
    """The CA that signs the public database ports, to verify the server."""
    import db_tls
    pem = await asyncio.to_thread(db_tls.ca_pem)
    return Response(pem, media_type="application/x-pem-file",
                    headers={"Content-Disposition": 'attachment; filename="cloudbase-database-ca.crt"'})


@router.get("/my-ip")
async def my_ip(request: Request, _user: dict = Depends(_auth.require_permission("databases.view"))):
    return {"ip": _client_ip(request)}


@router.get("")
async def list_databases(
    app_id: Optional[int] = Query(default=None),
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("databases.view")),
):
    query = select(Database).order_by(Database.name)
    if app_id is not None:
        query = query.join(DatabaseLink, DatabaseLink.database_id == Database.id).where(DatabaseLink.app_id == app_id)
    databases = (await db.execute(query)).scalars().all()
    nodes = {n.id: n for n in (await db.execute(select(Node))).scalars().all()}
    counts = dict((await db.execute(
        select(DatabaseLink.database_id, func.count()).group_by(DatabaseLink.database_id)
    )).all())
    return [_as_dict(d, nodes.get(d.node_id), counts.get(d.id, 0)) for d in databases]


@router.post("")
async def create_database(
    req: CreateDatabaseRequest,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("databases.create")),
    actor: str = Depends(_auth.get_current_actor),
):
    name = (req.name or "").strip().lower()
    if not NAME_RE.match(name):
        raise HTTPException(400, "Name must be 3–32 characters: lowercase letters, digits and dashes, starting with a letter")
    if name in db_engines.RESERVED_NAMES:
        raise HTTPException(400, f"'{name}' is reserved, pick another name")
    try:
        engine = db_engines.get_engine(req.engine)
    except ValueError as e:
        raise HTTPException(400, str(e))
    version = req.version or engine.versions[0]
    if version not in engine.versions:
        raise HTTPException(400, f"{engine.label} {version} is not offered. Choose one of: {', '.join(engine.versions)}")
    _validate_limits(engine.key, req.cpu_limit, req.memory_limit_mb)

    if req.node_id is not None:
        node = await db.get(Node, req.node_id)
        if node is None:
            raise HTTPException(404, "Node not found")
    else:
        node = (await db.execute(select(Node).where(Node.is_local == True))).scalar_one_or_none()
        if node is None:
            raise HTTPException(409, "No primary node registered yet")
    if not _node_reachable(node):
        raise HTTPException(409, f"Node '{node.name}' is offline or disabled")

    database = Database(
        id=await _next_database_id(db),
        name=name,
        engine=engine.key,
        version=version,
        node_id=node.id,
        status="creating",
        desired_state="running",
        credentials=encrypt_text(json.dumps(_new_credentials(name, engine.key))),
        cpu_limit=req.cpu_limit,
        memory_limit_mb=req.memory_limit_mb,
    )
    db.add(database)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, f"A database named '{name}' already exists")
    await log_audit(db, "database.create", actor=actor, detail={
        "database_id": database.id, "name": name, "engine": engine.key, "version": version, "node": node.name,
    })
    await db.commit()

    _spawn(database.id, lambda: _start_operation(database.id, "ensure"))
    return _as_dict(database, node)


@router.get("/{database_id}")
async def get_database(
    database_id: int,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("databases.view")),
):
    database = await _get_or_404(database_id, db)
    node = await db.get(Node, database.node_id)
    links = await _links_for(database_id, db)
    creds = db_links.decrypt_credentials(database)
    # User and database name are not secret; the password only comes from /credentials.
    return {**_as_dict(database, node, len(links)), "links": links, "user": creds["user"], "db_name": creds["db_name"]}


@router.patch("/{database_id}")
async def update_database(
    database_id: int,
    req: UpdateDatabaseRequest,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("databases.manage")),
    actor: str = Depends(_auth.get_current_actor),
):
    database = await _get_or_404(database_id, db)
    _require_idle(database)
    cpu = None if req.clear_cpu_limit else (req.cpu_limit if req.cpu_limit is not None else database.cpu_limit)
    mem = None if req.clear_memory_limit else (req.memory_limit_mb if req.memory_limit_mb is not None else database.memory_limit_mb)
    _validate_limits(database.engine, cpu, mem)
    if cpu == database.cpu_limit and mem == database.memory_limit_mb:
        return {"ok": True, "changed": False}

    node = await _node_for(database, db)
    if database.desired_state == "running" and not _node_reachable(node):
        raise HTTPException(409, f"Node '{node.name}' is offline; limits can only change while it is reachable")

    database.cpu_limit = cpu
    database.memory_limit_mb = mem
    await log_audit(db, "database.update", actor=actor, detail={
        "database_id": database.id, "name": database.name, "cpu_limit": cpu, "memory_limit_mb": mem,
    })
    if database.desired_state == "running":
        database.status = "starting"
        await db.commit()
        # New limits (and the tuning derived from them) need a new container; the volume stays.
        _spawn(database.id, lambda: _start_operation(database.id, "recreate"))
    else:
        await db.commit()
    return {"ok": True, "changed": True}


@router.post("/{database_id}/start")
async def start_database(
    database_id: int,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("databases.manage")),
    actor: str = Depends(_auth.get_current_actor),
):
    database = await _get_or_404(database_id, db)
    _require_idle(database)
    node = await _node_for(database, db)
    if not _node_reachable(node):
        raise HTTPException(409, f"Node '{node.name}' is offline")
    database.desired_state = "running"
    database.status = "starting"
    database.last_error = None
    await log_audit(db, "database.start", actor=actor, detail={"database_id": database.id, "name": database.name})
    await db.commit()
    _spawn(database.id, lambda: _start_operation(database.id, "ensure"))
    return {"ok": True}


@router.post("/{database_id}/restart")
async def restart_database(
    database_id: int,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("databases.manage")),
    actor: str = Depends(_auth.get_current_actor),
):
    database = await _get_or_404(database_id, db)
    _require_idle(database)
    node = await _node_for(database, db)
    if not _node_reachable(node):
        raise HTTPException(409, f"Node '{node.name}' is offline")
    database.desired_state = "running"
    database.status = "starting"
    database.last_error = None
    await log_audit(db, "database.restart", actor=actor, detail={"database_id": database.id, "name": database.name})
    await db.commit()
    _spawn(database.id, lambda: _start_operation(database.id, "restart"))
    return {"ok": True}


@router.post("/{database_id}/stop")
async def stop_database(
    database_id: int,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("databases.manage")),
    actor: str = Depends(_auth.get_current_actor),
):
    database = await _get_or_404(database_id, db)
    _require_idle(database)
    node = await _node_for(database, db)
    if not _node_reachable(node):
        raise HTTPException(409, f"Node '{node.name}' is offline")
    database.desired_state = "stopped"
    database.status = "stopping"
    database.last_error = None
    await log_audit(db, "database.stop", actor=actor, detail={"database_id": database.id, "name": database.name})
    await db.commit()

    async def op():
        await _run_on_node(node, lambda: db_manager.stop(database_id), "db_stop", {"database_id": database_id})
        await _set_status(database_id, "stopped")

    _spawn(database.id, op)
    return {"ok": True}


@router.delete("/{database_id}")
async def delete_database(
    database_id: int,
    keep_data: bool = Query(default=False),
    force: bool = Query(default=False),
    final_backup: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("databases.delete")),
    actor: str = Depends(_auth.get_current_actor),
):
    database = await _get_or_404(database_id, db)
    _require_idle(database)
    node = await db.get(Node, database.node_id)
    if final_backup and node is not None and _node_reachable(node) and database.status in ("running", "unhealthy"):
        from routers.database_backups import run_backup
        _busy.add(database_id)
        _operation[database_id] = "Making a final backup…"
        try:
            await run_backup(database_id, "pre_delete", actor)
        except Exception as e:
            raise HTTPException(502, f"The final backup failed, so the database was not deleted: {e}")
        finally:
            _busy.discard(database_id)
            _operation.pop(database_id, None)
    removed_on_node = False
    if node is not None and _node_reachable(node):
        _busy.add(database_id)
        try:
            await _run_on_node(
                node,
                lambda: db_manager.remove(database_id, keep_volume=keep_data),
                "db_remove",
                {"database_id": database_id, "keep_volume": keep_data},
            )
            removed_on_node = True
        except Exception as e:
            if not force:
                raise HTTPException(502, f"Could not remove the database container: {e}")
            log.warning("[db] forced delete of database %s; container removal failed: %s", database_id, e)
        finally:
            _busy.discard(database_id)
    elif not force:
        raise HTTPException(
            409,
            "The node of this database is offline. Delete again with force to remove it from Cloudbase only; "
            "the container and its data then stay on the node.",
        )

    name = database.name
    await db.delete(database)
    await log_audit(db, "database.delete", actor=actor, detail={
        "database_id": database_id, "name": name, "keep_data": keep_data, "removed_on_node": removed_on_node,
    })
    await db.commit()
    await db_links.refresh(db)
    await push_proxies()
    return {"ok": True}


@router.get("/{database_id}/credentials")
async def get_credentials(
    database_id: int,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("databases.credentials")),
    actor: str = Depends(_auth.get_current_actor),
):
    database = await _get_or_404(database_id, db)
    creds = db_links.decrypt_credentials(database)
    await log_audit(db, "database.credentials_view", actor=actor, detail={"database_id": database.id, "name": database.name})
    await db.commit()
    result = {
        "user": creds["user"],
        "password": creds["password"],
        "database": creds["db_name"],
        "host": db_manager.container_name(database.id),
        "port": db_engines.get_engine(database.engine).port,
        "url": db_links.connection_url(database, creds),
    }
    if database.engine in ("mysql", "mariadb"):
        result["root_password"] = creds["root_password"]
    public_port = _safe_public_port(database.id)
    if database.public_enabled and public_port and _public_host():
        engine = db_engines.get_engine(database.engine)
        url = engine.url(creds, _public_host(), public_port)
        result["public_url"] = engine.tls_url(url) if database.public_tls and engine.tls_url else url
        if database.public_tls and engine.tls_verify_url:
            result["public_url_verified"] = engine.tls_verify_url(url)
    return result


@router.put("/{database_id}/public")
async def set_public_access(
    database_id: int,
    req: PublicAccessRequest,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("databases.manage")),
    actor: str = Depends(_auth.get_current_actor),
):
    database = await _get_or_404(database_id, db)
    entries = [e.strip() for e in req.allowlist if e.strip()]
    try:
        db_proxy.parse_allowlist(entries)
    except ValueError as e:
        raise HTTPException(400, f"Invalid IP address or range: {e}")
    if req.enabled and _safe_public_port(database.id) is None:
        raise HTTPException(409, "This database id is outside the db_public port range in config.yaml")
    database.public_enabled = req.enabled
    database.public_allowlist = json.dumps(entries)
    if req.tls is not None:
        database.public_tls = req.tls
    await log_audit(db, "database.public_access", actor=actor, detail={
        "database_id": database.id, "name": database.name, "enabled": req.enabled, "allowlist": entries,
        "tls": bool(database.public_tls),
    })
    await db.commit()
    await push_proxies()
    node = await db.get(Node, database.node_id)
    links = await _links_for(database.id, db)
    return {**_as_dict(database, node, len(links)), "links": links}


@router.post("/{database_id}/rotate-password")
async def rotate_password(
    database_id: int,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("databases.credentials")),
    actor: str = Depends(_auth.get_current_actor),
):
    """Give the app user a new password. The old one stops working at once; linked
    apps pick up the new URL when they restart."""
    database = await _get_or_404(database_id, db)
    _require_idle(database)
    node = await _node_for(database, db)
    if not _node_reachable(node):
        raise HTTPException(409, f"Node '{node.name}' is offline")
    if database.status not in ("running", "unhealthy"):
        raise HTTPException(409, "The database must be running to change its password")

    spec = _spec(database)
    new_password = secrets.token_urlsafe(24)
    _busy.add(database_id)
    _operation[database_id] = "Changing the password…"
    try:
        await _run_on_node(
            node,
            lambda: db_manager.rotate_password(spec, new_password),
            "db_rotate_password",
            {"spec": spec, "new_password": new_password},
        )
        creds = {**spec["credentials"], "password": new_password}
        database.credentials = encrypt_text(json.dumps(creds))
        await log_audit(db, "database.rotate_password", actor=actor, detail={"database_id": database.id, "name": database.name})
        await db.commit()
    except Exception as e:
        raise HTTPException(502, f"The password was not changed: {e}")
    finally:
        _busy.discard(database_id)
        _operation.pop(database_id, None)

    await db_links.refresh(db)
    if database.engine in db_manager.ROTATE_RECREATES:
        # The server command / environment carries the password: new container, same data.
        database.status = "starting"
        await db.commit()
        _spawn(database.id, lambda: _start_operation(database.id, "recreate"))
    links = await _links_for(database_id, db)
    return {"ok": True, "apps": [{"id": l["app_id"], "name": l["app_name"]} for l in links]}


@router.get("/{database_id}/stats/history")
async def stats_history(
    database_id: int,
    hours: float = Query(default=24, ge=0.25, le=24 * 7),
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("databases.view")),
):
    """CPU, memory and data-size samples, oldest first, at most ~300 points."""
    since = datetime.utcnow() - timedelta(hours=hours)
    rows = (await db.execute(
        select(DatabaseStatsHistory)
        .where(DatabaseStatsHistory.database_id == database_id, DatabaseStatsHistory.timestamp >= since)
        .order_by(DatabaseStatsHistory.timestamp.asc())
    )).scalars().all()
    step = max(1, len(rows) // 300)
    samples = []
    for i in range(0, len(rows), step):
        chunk = rows[i:i + step]
        avg = lambda key: (lambda vals: round(sum(vals) / len(vals), 2) if vals else None)(
            [getattr(r, key) for r in chunk if getattr(r, key) is not None])
        samples.append({
            "ts": int(chunk[-1].timestamp.replace(tzinfo=timezone.utc).timestamp() * 1000),
            "cpu": avg("cpu_percent"), "mem": avg("memory_mb"), "disk": avg("disk_mb"),
        })
    return {"hours": hours, "samples": samples}


@router.post("/{database_id}/check-update")
async def check_update(
    database_id: int,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("databases.view")),
):
    database = await _get_or_404(database_id, db)
    node = await _node_for(database, db)
    if not _node_reachable(node):
        raise HTTPException(409, f"Node '{node.name}' is offline")
    await _check_updates_on(node, [database])
    return _update_info.get(database_id) or {}


@router.post("/{database_id}/update")
async def update_database_engine(
    database_id: int,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("databases.manage")),
    actor: str = Depends(_auth.get_current_actor),
):
    """Install the newest patch release of the database's version (e.g. 18.6 → 18.7).
    A backup is made first; a failed start rolls back to the previous image."""
    database = await _get_or_404(database_id, db)
    _require_idle(database)
    node = await _node_for(database, db)
    if not _node_reachable(node):
        raise HTTPException(409, f"Node '{node.name}' is offline")
    if database.status not in ("running", "unhealthy"):
        raise HTTPException(409, "The database must be running to update it")
    await log_audit(db, "database.update_engine", actor=actor, detail={
        "database_id": database.id, "name": database.name, "from": (_update_info.get(database_id) or {}).get("version"),
    })
    await db.commit()

    async def op():
        from routers.database_backups import run_backup
        _operation[database_id] = "Backing up before the update…"
        await run_backup(database_id, "pre_update", actor)
        _operation[database_id] = "Updating…"
        async with AsyncSessionLocal() as s:
            spec = _spec(await s.get(Database, database_id))
        try:
            result = await _run_on_node(node, lambda: db_manager.update(spec), "db_update", {"spec": spec},
                                        timeout=PROVISION_TIMEOUT_SECONDS + 900)
        finally:
            _operation.pop(database_id, None)
        _update_info[database_id] = {
            "version": result.get("version"), "update_available": False,
            "checked_at": datetime.utcnow().isoformat(), "updated_from": result.get("from"),
        }
        await _set_status(database_id, "running")

    _spawn(database.id, op)
    return {"ok": True}


@router.get("/{database_id}/logs")
async def get_logs(
    database_id: int,
    lines: int = Query(default=300, ge=10, le=2000),
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("databases.view")),
):
    database = await _get_or_404(database_id, db)
    node = await _node_for(database, db)
    try:
        result = await _run_on_node(
            node,
            lambda: {"lines": db_manager.logs(database_id, lines)},
            "db_logs",
            {"database_id": database_id, "lines": lines},
            timeout=20,
        )
    except Exception as e:
        raise HTTPException(502, str(e))
    return {"lines": result.get("lines") or []}


@router.get("/{database_id}/stats")
async def get_stats(
    database_id: int,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("databases.view")),
):
    database = await _get_or_404(database_id, db)
    node = await _node_for(database, db)
    data_dir = _data_dir(database)
    try:
        return await _run_on_node(
            node,
            lambda: db_manager.stats(database_id, data_dir),
            "db_stats",
            {"database_id": database_id, "data_dir": data_dir},
            timeout=20,
        )
    except Exception as e:
        raise HTTPException(502, str(e))


@router.post("/{database_id}/links")
async def create_link(
    database_id: int,
    req: LinkRequest,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("databases.manage")),
    actor: str = Depends(_auth.get_current_actor),
):
    database = await _get_or_404(database_id, db)
    app = await db.get(Application, req.app_id)
    if app is None:
        raise HTTPException(404, "App not found")
    app_name = app.name  # read before a possible rollback expires the instance
    env_var = (req.env_var_name or db_engines.get_engine(database.engine).env_var).strip().upper()
    if not ENV_VAR_RE.match(env_var):
        raise HTTPException(400, "Env var name may only contain A–Z, 0–9 and _, and may not start with a digit")
    if env_var in decrypt_env(app.env_vars or ""):
        raise HTTPException(409, f"{app_name} already sets {env_var} itself. Remove it there or pick another name.")

    link = DatabaseLink(database_id=database.id, app_id=app.id, env_var_name=env_var)
    db.add(link)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, f"{app_name} already gets {env_var} from a linked database")
    await log_audit(db, "database.link", actor=actor, app_id=app.id, detail={
        "database_id": database.id, "name": database.name, "env_var": env_var,
    })
    await db.commit()
    await db_links.refresh(db)
    await push_proxies()
    return {"ok": True, "id": link.id, "env_var_name": env_var, "restart_required": True}


@router.delete("/{database_id}/links/{link_id}")
async def delete_link(
    database_id: int,
    link_id: int,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("databases.manage")),
    actor: str = Depends(_auth.get_current_actor),
):
    link = await db.get(DatabaseLink, link_id)
    if link is None or link.database_id != database_id:
        raise HTTPException(404, "Link not found")
    await db.delete(link)
    await log_audit(db, "database.unlink", actor=actor, app_id=link.app_id, detail={
        "database_id": database_id, "env_var": link.env_var_name,
    })
    await db.commit()
    await db_links.refresh(db)
    await push_proxies()
    return {"ok": True, "restart_required": True}


# ── Monitor ───────────────────────────────────────────────────────────────────

async def _fetch_states(node: Node, ids: list[int]) -> dict[str, dict]:
    result = await _run_on_node(
        node,
        lambda: {"states": db_manager.states(ids)},
        "db_states",
        {"database_ids": ids},
        timeout=30,
    )
    return result.get("states") or {}


async def _monitor_tick() -> None:
    async with AsyncSessionLocal() as db:
        databases = (await db.execute(select(Database))).scalars().all()
        nodes = {n.id: n for n in (await db.execute(select(Node))).scalars().all()}

    by_node: dict[int, list[Database]] = {}
    for d in databases:
        by_node.setdefault(d.node_id, []).append(d)

    for node_id, items in by_node.items():
        node = nodes.get(node_id)
        idle = [d for d in items if d.id not in _busy]
        if not idle:
            continue
        if node is None or not _node_reachable(node):
            for d in idle:
                if d.status != "node_offline":
                    await _set_status(d.id, "node_offline", d.last_error)
            continue
        try:
            states = await _fetch_states(node, [d.id for d in idle])
        except Exception as e:
            log.debug("[db-monitor] state check on node %s failed: %s", node.name, e)
            continue

        for d in idle:
            if d.id in _busy:
                continue
            info = states.get(str(d.id)) or {}
            state = info.get("state", "missing")

            if d.desired_state == "stopped":
                if state in ("stopped", "missing") and d.status != "stopped":
                    await _set_status(d.id, "stopped")
                elif state not in ("stopped", "missing"):
                    status, error = _state_to_status(state)
                    await _set_status(d.id, status, error)
                continue

            if state == "missing" and d.initialized_at is not None and not info.get("volume", True):
                message = (f"Data volume {db_manager.volume_name(d.id)} is missing on node '{node.name}'. "
                           "Cloudbase will not create an empty database in its place.")
                if d.status != "error" or d.last_error != message:
                    await _set_status(d.id, "error", message)
                continue

            if state in ("stopped", "missing"):
                # Should be running but is not: start it again, with a backoff.
                now = time.monotonic()
                if now - _last_heal.get(d.id, 0) < HEAL_BACKOFF_SECONDS:
                    continue
                _last_heal[d.id] = now
                log.info("[db-monitor] database %s (%s) is %s, starting it", d.name, d.id, state)
                db_id = d.id
                _spawn(db_id, lambda db_id=db_id: _start_operation(db_id, "ensure"))
                continue

            status, error = _state_to_status(state)
            if status != d.status or (error or None) != (d.last_error or None) or (status == "running" and d.initialized_at is None):
                await _set_status(d.id, status, error)


async def database_monitor() -> None:
    """Keep database statuses in sync with Docker on every node (primary only)."""
    async with AsyncSessionLocal() as db:
        await db_links.refresh(db)
    db_proxy.configure(asyncio.get_running_loop(), open_db_stream)
    await asyncio.sleep(10)
    while True:
        try:
            await _monitor_tick()
            await push_proxies()
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.warning("[db-monitor] error: %s", e)
        await asyncio.sleep(MONITOR_INTERVAL_SECONDS)


# ── Connections across nodes ──────────────────────────────────────────────────
#
#   app on node X ─► proxy on X ─WS─► primary ─► database on the primary
#                                             └─WS (dial)─► database on node Y
#
# One WebSocket per database connection, binary frames, no multiplexing.

async def _compute_desired_proxies() -> dict[int, set[int]]:
    """Which databases each node must proxy: those linked to an app with an
    instance on that node, when the database itself lives elsewhere."""
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(DatabaseLink.app_id, Database.id, Database.node_id)
            .join(Database, Database.id == DatabaseLink.database_id)
        )).all()
        if not rows:
            return {}
        app_ids = {r[0] for r in rows}
        replica_rows = (await db.execute(
            select(ApplicationReplica.app_id, ApplicationReplica.node_id).where(ApplicationReplica.app_id.in_(app_ids))
        )).all()
        app_rows = (await db.execute(
            select(Application.id, Application.node_id).where(Application.id.in_(app_ids))
        )).all()
    nodes_by_app: dict[int, set[int]] = {}
    for app_id, node_id in replica_rows:
        if node_id is not None:
            nodes_by_app.setdefault(app_id, set()).add(node_id)
    for app_id, node_id in app_rows:
        if node_id is not None and app_id not in nodes_by_app:
            nodes_by_app[app_id] = {node_id}
    desired: dict[int, set[int]] = {}
    for app_id, db_id, db_node_id in rows:
        for node_id in nodes_by_app.get(app_id, ()):
            if node_id != db_node_id:
                desired.setdefault(node_id, set()).add(db_id)
    return desired


async def _send_control(node_id: int, message: dict) -> bool:
    entry = _control.get(node_id)
    if entry is None:
        return False
    ws, lock = entry
    try:
        async with lock:
            await ws.send_text(json.dumps(message))
        return True
    except Exception:
        return False


async def push_proxies() -> None:
    """Send every node (and the primary itself) the proxies it should run."""
    global _desired_proxies
    _desired_proxies = await _compute_desired_proxies()
    local_id = db_proxy.local_node_id()
    if local_id is not None:
        await db_proxy.reconcile(sorted(_desired_proxies.get(local_id, set())))
    async with AsyncSessionLocal() as db:
        public = (await db.execute(select(Database).where(Database.public_enabled == True))).scalars().all()
        local = (await db.execute(select(Node).where(Node.is_local == True))).scalar_one_or_none()
    global _public_ip
    _public_ip = local.public_host if local else None
    hosts = [h for h in (_public_ip, "localhost") if h]
    await db_proxy.reconcile_public([
        {"database_id": d.id, "allowlist": _allowlist(d), "engine": d.engine,
         "tls": bool(d.public_tls), "hosts": hosts}
        for d in public
    ])
    for node_id in list(_control):
        await _send_control(node_id, {"type": "proxies", "database_ids": sorted(_desired_proxies.get(node_id, set()))})


async def open_db_stream(database_id: int) -> db_proxy.Stream:
    """A byte stream to a database, wherever it runs."""
    async with AsyncSessionLocal() as db:
        database = await db.get(Database, database_id)
        node = await db.get(Node, database.node_id) if database else None
    if database is None or node is None:
        raise RuntimeError(f"Database {database_id} not found")

    if node.is_local:
        host, port = await asyncio.to_thread(db_manager.container_address, database_id)
        reader, writer = await asyncio.open_connection(host, port)
        return db_proxy.TcpStream(reader, writer)

    token = secrets.token_urlsafe(18)
    future = asyncio.get_running_loop().create_future()
    _pending_dials[token] = (future, node.id)
    try:
        if not await _send_control(node.id, {"type": "dial", "token": token, "database_id": database_id}):
            raise RuntimeError(f"Node '{node.name}' has no database control connection")
        return await asyncio.wait_for(future, timeout=DIAL_TIMEOUT_SECONDS)
    finally:
        _pending_dials.pop(token, None)


async def _authorize_node(websocket: WebSocket) -> Optional[Node]:
    token = websocket.headers.get("x-node-token") or websocket.query_params.get("token")
    if not token:
        await websocket.close(code=1008, reason="Missing node token")
        return None
    async with AsyncSessionLocal() as db:
        try:
            return await get_node_by_token_or_401(token, db)
        except HTTPException:
            await websocket.close(code=1008, reason="Invalid node token")
            return None


@relay_router.websocket("/db-control")
async def db_control_ws(websocket: WebSocket):
    """Per-node control channel: proxy lists and dial requests go out here."""
    node = await _authorize_node(websocket)
    if node is None:
        return
    await websocket.accept()
    old = _control.get(node.id)
    _control[node.id] = (websocket, asyncio.Lock())
    if old is not None:
        try:
            await old[0].close(code=1012)
        except Exception:
            pass
    log.info("[db-relay] control channel of node %s connected", node.name)
    try:
        await push_proxies()  # fresh list, e.g. after the node restarted
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            try:
                data = json.loads(msg.get("text") or "{}")
            except ValueError:
                continue
            if data.get("type") == "progress":
                _remote_progress[node.id] = {str(k): str(v) for k, v in (data.get("progress") or {}).items()}
    except Exception:
        pass
    finally:
        current = _control.get(node.id)
        if current is not None and current[0] is websocket:
            _control.pop(node.id, None)
            _remote_progress.pop(node.id, None)
        log.info("[db-relay] control channel of node %s disconnected", node.name)


@relay_router.websocket("/db-dial/{token}")
async def db_dial_ws(token: str, websocket: WebSocket):
    """A database's node answering a dial: this socket carries one connection."""
    node = await _authorize_node(websocket)
    if node is None:
        return
    pending = _pending_dials.get(token)
    if pending is None or pending[1] != node.id or pending[0].done():
        await websocket.close(code=1008, reason="Unknown or expired dial")
        return
    await websocket.accept()
    stream = db_proxy.ServerWebSocketStream(websocket)
    pending[0].set_result(stream)
    await stream.closed.wait()


@relay_router.websocket("/db-proxy/{database_id}")
async def db_proxy_ws(database_id: int, websocket: WebSocket):
    """One database connection from an app instance on a node."""
    node = await _authorize_node(websocket)
    if node is None:
        return
    if database_id not in _desired_proxies.get(node.id, set()):
        await push_proxies()  # an instance may have started since the last push
        if database_id not in _desired_proxies.get(node.id, set()):
            await websocket.close(code=1008, reason="No linked app on this node uses this database")
            return
    await websocket.accept()
    client = db_proxy.ServerWebSocketStream(websocket)
    try:
        upstream = await open_db_stream(database_id)
    except Exception as e:
        log.warning("[db-relay] database %s for node %s: %s", database_id, node.name, e)
        await client.close()
        return
    await db_proxy.pipe(client, upstream)


# ── Usage history ─────────────────────────────────────────────────────────────

STATS_INTERVAL_SECONDS = 60
STATS_DISK_EVERY = 10          # measure the data size every 10th sample (du is slow)
STATS_RETENTION_DAYS = 7


async def _stats_tick(with_disk: bool) -> None:
    async with AsyncSessionLocal() as db:
        databases = (await db.execute(
            select(Database).where(Database.status.in_(["running", "unhealthy", "starting"]))
        )).scalars().all()
        nodes = {n.id: n for n in (await db.execute(select(Node))).scalars().all()}
    by_node: dict[int, list[dict]] = {}
    for d in databases:
        by_node.setdefault(d.node_id, []).append(
            {"database_id": d.id, "data_dir": _data_dir(d) if with_disk else None})
    now = datetime.utcnow()
    rows = []
    for node_id, items in by_node.items():
        node = nodes.get(node_id)
        if node is None or not _node_reachable(node):
            continue
        try:
            result = await _run_on_node(node, lambda items=items: {"stats": db_manager.stats_many(items)},
                                        "db_stats_many", {"items": items}, timeout=60)
        except Exception as e:
            log.debug("[db-stats] node %s: %s", node.name, e)
            continue
        for db_id, s in (result.get("stats") or {}).items():
            rows.append(DatabaseStatsHistory(
                database_id=int(db_id), timestamp=now,
                cpu_percent=s.get("cpu_percent"), memory_mb=s.get("memory_mb"), disk_mb=s.get("disk_mb"),
            ))
    if rows:
        async with AsyncSessionLocal() as db:
            db.add_all(rows)
            await db.commit()


async def database_stats_sampler() -> None:
    """Sample database usage every minute for the history charts (primary only)."""
    from sqlalchemy import delete as _delete
    await asyncio.sleep(45)
    tick = 0
    while True:
        try:
            await _stats_tick(with_disk=tick % STATS_DISK_EVERY == 0)
            if tick % 60 == 0:
                async with AsyncSessionLocal() as db:
                    await db.execute(_delete(DatabaseStatsHistory).where(
                        DatabaseStatsHistory.timestamp < datetime.utcnow() - timedelta(days=STATS_RETENTION_DAYS)))
                    await db.commit()
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.warning("[db-stats] error: %s", e)
        tick += 1
        await asyncio.sleep(STATS_INTERVAL_SECONDS)



# ── Update checks ─────────────────────────────────────────────────────────────

UPDATE_CHECK_INTERVAL_SECONDS = 12 * 3600


async def _check_updates_on(node: Node, databases: list) -> None:
    items = [{"database_id": d.id, "ref": db_engines.get_engine(d.engine).image_ref(d.version)} for d in databases]
    result = await _run_on_node(node, lambda: {"updates": db_manager.check_updates(items)},
                                "db_check_updates", {"items": items}, timeout=120)
    now = datetime.utcnow().isoformat()
    for db_id, info in (result.get("updates") or {}).items():
        _update_info[int(db_id)] = {**info, "checked_at": now}


async def database_update_checker() -> None:
    """Look for newer patch releases twice a day (primary only)."""
    await asyncio.sleep(300)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                databases = (await db.execute(
                    select(Database).where(Database.status.in_(["running", "unhealthy"]))
                )).scalars().all()
                nodes = {n.id: n for n in (await db.execute(select(Node))).scalars().all()}
            by_node: dict[int, list] = {}
            for d in databases:
                by_node.setdefault(d.node_id, []).append(d)
            for node_id, items in by_node.items():
                node = nodes.get(node_id)
                if node is not None and _node_reachable(node):
                    try:
                        await _check_updates_on(node, items)
                    except Exception as e:
                        log.debug("[db-updates] node %s: %s", node.name, e)
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.warning("[db-updates] error: %s", e)
        await asyncio.sleep(UPDATE_CHECK_INTERVAL_SECONDS)
