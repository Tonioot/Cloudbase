import json
import asyncio
import secrets
import time
from datetime import timedelta
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy import delete, select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import AsyncSessionLocal, get_db
from core.models import Node, NodeInvite, NodeCommand
from core.audit import log_audit
from core import auth as _auth
from nodes.commands import _utcnow, queue_node_command, wait_for_node_command
from nodes.recovery import run_node_docker_cleanup
from nodes.registry import _as_node_dict, ensure_local_node, get_node_or_404
from nodes.state import _node_ws_connections, _pending_pings, _push_node_event
from routers.nodes.schemas import InviteRequest, NodeUpdateRequest
from core.models import Application, ApplicationReplica
from core.models import Database
from core import system_logs

router = APIRouter(prefix="/api/nodes", tags=["nodes"])


@router.get("")
async def list_nodes(db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("nodes.view"))):
    await ensure_local_node(db)
    result = await db.execute(select(Node).order_by(Node.is_local.desc(), Node.name.asc()))
    nodes = result.scalars().all()
    return [_as_node_dict(n) for n in nodes]


@router.post("/invites")
async def create_invite(req: InviteRequest, _user: dict = Depends(_auth.require_permission("nodes.add")), db: AsyncSession = Depends(get_db)):
    code = secrets.token_urlsafe(24)
    invite = NodeInvite(
        code=code,
        note=req.note,
        expires_at=_utcnow() + timedelta(minutes=req.ttl_minutes),
    )
    db.add(invite)
    await db.commit()
    await db.refresh(invite)
    return {
        "code": invite.code,
        "note": invite.note,
        "expires_at": invite.expires_at.isoformat() + "Z",  # Append Z to indicate UTC
    }


@router.post("/{node_id}/enable")
async def enable_node(node_id: int, db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("nodes.configure")), actor: str = Depends(_auth.get_current_actor)):
    node = await get_node_or_404(node_id, db)
    node.enabled = True
    await log_audit(db, "node.enable", actor=actor, detail={"name": node.name, "node_id": node_id})
    await db.commit()
    return _as_node_dict(node)


@router.post("/{node_id}/disable")
async def disable_node(node_id: int, db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("nodes.configure")), actor: str = Depends(_auth.get_current_actor)):
    node = await get_node_or_404(node_id, db)
    if node.is_local:
        raise HTTPException(400, "Local node cannot be disabled")
    node.enabled = False
    node.status = "offline"
    if node.offline_since is None:
        node.offline_since = _utcnow()
    await log_audit(db, "node.disable", actor=actor, detail={"name": node.name, "node_id": node_id})
    await db.commit()
    return _as_node_dict(node)


@router.patch("/{node_id}")
async def update_node(node_id: int, req: NodeUpdateRequest, db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("nodes.configure")), actor: str = Depends(_auth.get_current_actor)):
    node = await get_node_or_404(node_id, db)
    old_name = node.name
    if req.name is not None:
        name = req.name.strip()
        if not name:
            raise HTTPException(400, "Name cannot be empty")
        existing = await db.execute(select(Node).where(Node.name == name, Node.id != node_id))
        if existing.scalar_one_or_none():
            raise HTTPException(409, "A node with that name already exists")
        node.name = name
    await log_audit(db, "node.rename", actor=actor, detail={"old_name": old_name, "new_name": node.name, "node_id": node_id})
    await db.commit()
    await db.refresh(node)
    return _as_node_dict(node)


@router.delete("/{node_id}")
async def delete_node(node_id: int, db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("nodes.delete")), actor: str = Depends(_auth.get_current_actor)):
    node = await get_node_or_404(node_id, db)
    if node.is_local:
        raise HTTPException(400, "Local node cannot be deleted")
    app_count_result = await db.execute(
        select(func.count()).where(Application.node_id == node_id)
    )
    app_count = app_count_result.scalar() or 0
    replica_count_result = await db.execute(
        select(func.count()).where(ApplicationReplica.node_id == node_id)
    )
    replica_count = replica_count_result.scalar() or 0
    database_count_result = await db.execute(
        select(func.count()).where(Database.node_id == node_id)
    )
    database_count = database_count_result.scalar() or 0
    if app_count > 0 or replica_count > 0 or database_count > 0:
        details = []
        if app_count > 0:
            details.append(f"{app_count} app{'s' if app_count != 1 else ''}")
        if replica_count > 0:
            details.append(f"{replica_count} instance{'s' if replica_count != 1 else ''}")
        if database_count > 0:
            details.append(f"{database_count} database{'s' if database_count != 1 else ''}")
        raise HTTPException(
            400,
            f"Cannot remove node: {', '.join(details)} still assigned to it. "
            "Move or delete all apps, instances and databases first."
        )
    node_name = node.name
    await db.execute(delete(NodeCommand).where(NodeCommand.node_id == node_id))
    await db.execute(delete(NodeInvite).where(NodeInvite.node_id == node_id))
    await db.delete(node)
    await log_audit(db, "node.delete", actor=actor, detail={"name": node_name, "node_id": node_id})
    await db.commit()
    return {"ok": True}


@router.post("/{node_id}/docker-cleanup")
async def docker_cleanup(
    node_id: int,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("nodes.configure")),
    actor: str = Depends(_auth.get_current_actor),
):
    """Free disk space now: remove superseded images and trim the build cache."""
    node = await get_node_or_404(node_id, db)
    res = await run_node_docker_cleanup(node, aggressive=True)
    await log_audit(db, "node.docker_cleanup", actor=actor, detail={"name": node.name, "node_id": node_id,
                                                                    "summary": res.get("summary") or res.get("skipped")})
    await db.commit()
    return res


@router.post("/{node_id}/ping")
async def ping_node(node_id: int, _user: dict = Depends(_auth.require_permission("nodes.view")), db: AsyncSession = Depends(get_db)):
    node = await get_node_or_404(node_id, db)

    if node.is_local:
        return {"reachable": True, "latency_ms": 0, "transport": "local"}

    if node.status == "offline" or not node.enabled:
        return {"reachable": False, "latency_ms": None, "transport": "none", "error": "Node is offline"}

    ws = _node_ws_connections.get(node_id)
    if ws:
        ping_id = secrets.token_hex(4)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        _pending_pings[ping_id] = fut
        t0 = time.monotonic()
        try:
            await ws.send_json({"type": "ping", "ping_id": ping_id})
            await asyncio.wait_for(fut, timeout=5.0)
            latency_ms = round((time.monotonic() - t0) * 1000)
            return {"reachable": True, "latency_ms": latency_ms, "transport": "websocket"}
        except asyncio.TimeoutError:
            # WS is stale — evict it and mark node offline immediately
            _node_ws_connections.pop(node_id, None)
            async with AsyncSessionLocal() as wdb:
                stale = await wdb.get(Node, node_id)
                if stale:
                    stale.status = "offline"
                    stale.connection_type = None
                    if stale.offline_since is None:
                        stale.offline_since = _utcnow()
                    await wdb.commit()
            _push_node_event(node_id, {"type": "node_offline", "node_id": node_id})
            return {"reachable": False, "latency_ms": None, "transport": "websocket", "error": "Ping timed out — node marked offline"}
        except Exception as exc:
            return {"reachable": False, "latency_ms": None, "transport": "websocket", "error": str(exc)}
        finally:
            _pending_pings.pop(ping_id, None)

    if node.api_base_url:
        import httpx
        try:
            t0 = time.monotonic()
            async with httpx.AsyncClient() as client:
                resp = await asyncio.wait_for(
                    client.get(f"{node.api_base_url}/api/health", timeout=5),
                    timeout=6,
                )
            latency_ms = round((time.monotonic() - t0) * 1000)
            return {"reachable": resp.status_code == 200, "latency_ms": latency_ms, "transport": "http"}
        except Exception as exc:
            return {"reachable": False, "latency_ms": None, "transport": "http", "error": str(exc)}

    return {"reachable": False, "latency_ms": None, "transport": "none", "error": "No active connection"}


@router.get("/{node_id}/connection-status")
async def get_node_connection_status(node_id: int, db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("nodes.view"))):
    node = await get_node_or_404(node_id, db)
    pending_count_result = await db.execute(
        select(func.count()).where(
            and_(NodeCommand.node_id == node_id, NodeCommand.status.in_(["queued", "in_progress"]))
        )
    )
    pending_count = pending_count_result.scalar() or 0
    ws_connected = node_id in _node_ws_connections
    return {
        "node_id": node_id,
        "websocket_connected": ws_connected,
        "connection_type": node.connection_type or ("websocket" if ws_connected else "http_polling"),
        "agent_version": node.agent_version,
        "last_seen": node.last_seen.isoformat() + "Z" if node.last_seen else None,
        "node_metrics": {
            "cpu_percent": node.node_cpu_percent,
            "memory_percent": node.node_memory_percent,
            "disk_percent": node.node_disk_percent,
        } if node.node_cpu_percent is not None else None,
        "pending_commands": pending_count,
    }


@router.get("/{node_id}/agent-logs")
async def get_node_agent_logs(node_id: int, limit: int = 200, db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("nodes.view"))):
    node = await get_node_or_404(node_id, db)
    if node.is_local:
        # The primary runs no agent loop; its activity is in the server log.
        try:
            lines = await asyncio.to_thread(system_logs._read_backwards, system_logs.SERVER_LOG, 2 * 1024 * 1024)
            return {"lines": lines[-limit:], "source": "local"}
        except FileNotFoundError:
            return {"lines": [], "source": "local", "error": "Log file not found"}
        except Exception as exc:
            return {"lines": [], "source": "local", "error": str(exc)}

    # Remote: queue a command and wait for result
    cmd = await queue_node_command(
        db,
        node_id=node_id,
        command_type="get_agent_logs",
        payload={"limit": max(1, min(limit, 2000))},
    )
    try:
        cmd = await wait_for_node_command(db, cmd.id, timeout_seconds=20.0)
        result = json.loads(cmd.result or "{}") if cmd.result else {}
        return {"lines": result.get("lines", []), "source": "remote"}
    except HTTPException:
        return {"lines": [], "source": "remote", "error": "Timed out waiting for agent logs"}
