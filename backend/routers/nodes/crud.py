import asyncio
import secrets
import time
from datetime import timedelta
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy import delete, select, func
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import get_db
from core.models import Node, NodeInvite
from core.audit import log_audit
from core import auth as _auth
from mesh import hub
from nodes import commands as node_commands
from nodes.commands import _utcnow
from nodes import workloads as node_workloads
from nodes.liveness import node_gone, run_node_docker_cleanup
from nodes import updates as node_updates
from nodes.registry import _as_node_dict, ensure_local_node, get_node_or_404
from nodes.state import _node_ws_connections, _pending_pings
from routers.nodes.schemas import InviteRequest, NodeUpdateRequest
from core.models import Application, Instance
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
        select(func.count()).where(Application.node_id == node_id, Application.cron_schedule.isnot(None))   # cron jobs that run there
    )
    app_count = app_count_result.scalar() or 0
    instance_count_result = await db.execute(
        select(func.count()).where(Instance.node_id == node_id)
    )
    instance_count = instance_count_result.scalar() or 0
    database_count_result = await db.execute(
        select(func.count()).where(Database.node_id == node_id)
    )
    database_count = database_count_result.scalar() or 0
    if app_count > 0 or instance_count > 0 or database_count > 0:
        details = []
        if app_count > 0:
            details.append(f"{app_count} cron job{'s' if app_count != 1 else ''}")
        if instance_count > 0:
            details.append(f"{instance_count} instance{'s' if instance_count != 1 else ''}")
        if database_count > 0:
            details.append(f"{database_count} database{'s' if database_count != 1 else ''}")
        raise HTTPException(
            400,
            f"Cannot remove node: {', '.join(details)} still assigned to it. "
            "Move or delete all apps, instances and databases first."
        )
    node_name = node.name
    await db.execute(delete(NodeInvite).where(NodeInvite.node_id == node_id))
    await db.delete(node)
    await log_audit(db, "node.delete", actor=actor, detail={"name": node_name, "node_id": node_id})
    await db.commit()
    node_commands.forget(node_id)
    await hub.sync_peers()
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


@router.post("/{node_id}/update")
async def update_cloudbase(
    node_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(_auth.require_permission("nodes.configure")),
    actor: str = Depends(_auth.get_current_actor),
):
    """Update Cloudbase on this node from Git and restart it. For the primary that restarts the panel."""
    node = await get_node_or_404(node_id, db)
    if node.is_local and user.get("permissions") is not None and "system.manage" not in user["permissions"]:
        raise HTTPException(403, "Updating the primary restarts the panel: that needs the system.manage permission")
    res = await node_updates.update_node(node)
    await log_audit(db, "node.update", actor=actor, detail={"name": node.name, "node_id": node_id,
                                                           "status": res.get("status"), "commit": res.get("commit")})
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
            await node_gone(node_id)
            return {"reachable": False, "latency_ms": None, "transport": "websocket", "error": "Ping timed out — node marked offline"}
        except Exception as exc:
            return {"reachable": False, "latency_ms": None, "transport": "websocket", "error": str(exc)}
        finally:
            _pending_pings.pop(ping_id, None)

    return {"reachable": False, "latency_ms": None, "transport": "none", "error": "No active connection"}


@router.get("/{node_id}/workloads")
async def get_node_workloads(node_id: int, db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("nodes.view"))):
    """The instances and databases that run on this node, for its page."""
    return await node_workloads.workloads(db, await get_node_or_404(node_id, db))


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

    try:
        cmd = await node_commands.ask(node_id, "get_agent_logs", {"limit": max(1, min(limit, 2000))})
        if cmd.status != "done":
            return {"lines": [], "source": "remote", "error": cmd.error_message or "The node could not read its log"}
        return {"lines": (cmd.result or {}).get("lines", []), "source": "remote"}
    except HTTPException:
        return {"lines": [], "source": "remote", "error": "Timed out waiting for agent logs"}
