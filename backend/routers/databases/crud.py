import asyncio
import json
import logging
from dbs.create import create_database_now
from datetime import timedelta, datetime, timezone
from typing import Optional
from fastapi import Request, Query, Response, HTTPException, APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from core import auth as _auth
from dbs import engines as db_engines, links as db_links, proxy as db_proxy, tls as db_tls
from dbs import docker as dbs_docker
from dbs import lifecycle as dbs_lifecycle
from core.audit import log_audit
from core.database import get_db
from core.models import DatabaseStatsHistory, DatabaseLink, Database, Node
from dbs.ops import _get_or_404, _links_for, _node_for, _node_reachable, _require_idle, _run_on_node, _spawn, _start_operation
from dbs.provision import _data_dir, _validate_limits
from dbs.relay import _public_host, push_proxies
from dbs.state import _busy, _operation
from dbs.view import _as_dict, _client_ip, _safe_public_port
from routers.databases.schemas import CreateDatabaseRequest, PublicAccessRequest, UpdateDatabaseRequest
from dbs.backups import run_backup

router = APIRouter(prefix="/api/databases", tags=["databases"])


log = logging.getLogger("cloudbase.databases")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/engines")
async def list_engines(_user: dict = Depends(_auth.require_permission("databases.view"))):
    return db_engines.catalog()


@router.get("/tls-ca.crt")
async def tls_ca(_user: dict = Depends(_auth.require_permission("databases.view"))):
    """The CA that signs the public database ports, to verify the server."""
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
    database, node = await create_database_now(
        db, name_in=req.name, engine_in=req.engine, version_in=req.version, node_id=req.node_id,
        cpu_limit=req.cpu_limit, memory_limit_mb=req.memory_limit_mb, actor=actor)
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
                lambda: dbs_lifecycle.remove(database_id, keep_volume=keep_data),
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
        "host": dbs_docker.container_name(database.id),
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
            lambda: {"lines": dbs_lifecycle.logs(database_id, lines)},
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
            lambda: dbs_lifecycle.stats(database_id, data_dir),
            "db_stats",
            {"database_id": database_id, "data_dir": data_dir},
            timeout=20,
        )
    except Exception as e:
        raise HTTPException(502, str(e))
