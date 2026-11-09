import json
import asyncio
import logging
import secrets
from typing import Optional
from fastapi import APIRouter, HTTPException, Header, WebSocket, WebSocketDisconnect, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import AsyncSessionLocal, get_db
from core.models import Node, NodeInvite
from core.audit import log_audit
from mesh import hub
from core import config as _cfg, system_config as _syscfg
from nodes import metrics as node_metrics, observed, snapshot
from nodes.liveness import node_gone
from nodes.commands import _apply_command_result, _claim_pending_commands, _commit_with_sqlite_lock_retry, _dispatch_queued_commands_ws, _utcnow, fail_commands_in_flight
from nodes.registry import _as_node_dict, get_node_by_token_or_401
from nodes.state import _get_node_event, _node_event_subscribers, _node_ws_connections, _pending_pings, _push_node_event, push_node_stream_data
from routers.nodes.schemas import CommandResultRequest, ObservedRequest, RegisterNodeRequest, WireguardEnrollRequest

log = logging.getLogger("cloudbase.nodes")

# node id -> the session id its agent sent when it connected (a new process sends a new one)
_node_sessions: dict[int, str] = {}


router = APIRouter(prefix="/api/nodes", tags=["nodes"])


@router.post("/agent/register")
async def register_node(req: RegisterNodeRequest, db: AsyncSession = Depends(get_db)):
    now = _utcnow()
    result = await db.execute(select(NodeInvite).where(NodeInvite.code == req.invite_code))
    invite = result.scalar_one_or_none()
    if not invite:
        raise HTTPException(400, "Invalid invite code")
    if invite.used_at is not None:
        raise HTTPException(400, "Invite code already used")
    if invite.expires_at < now:
        raise HTTPException(400, "Invite code expired")

    existing = await db.execute(select(Node).where(Node.name == req.name))
    if existing.scalar_one_or_none():
        raise HTTPException(400, f"Node '{req.name}' already exists")

    node_count_result = await db.execute(select(func.count()).select_from(Node))
    if node_count_result.scalar() >= _cfg.get_limit("max_nodes"):
        raise HTTPException(400, f"Node limit reached ({_cfg.get_limit('max_nodes')} nodes maximum). Adjust limits.max_nodes in config.yaml to increase.")

    auth_token = secrets.token_urlsafe(40)
    node = Node(
        name=req.name,
        role=req.role if req.role in ("main", "node", "hybrid") else "node",
        status="online",
        api_base_url=req.api_base_url,
        public_host=req.public_host,
        auth_token=auth_token,
        is_local=False,
        enabled=True,
        heartbeat_interval=req.heartbeat_interval,
        capabilities=json.dumps(req.capabilities or {}),
        metadata_json=json.dumps(req.metadata_json or {}),
        last_seen=now,
    )
    db.add(node)
    await db.flush()

    invite.used_at = now
    invite.node_id = node.id

    await log_audit(db, "node.connect", actor="agent", detail={"name": node.name, "node_id": node.id})
    await db.commit()
    await db.refresh(node)

    # Broadcast to all browser subscribers that a new node connected
    for qlist in list(_node_event_subscribers.values()):
        for q in qlist:
            try:
                q.put_nowait({"type": "node_connected", "node_id": node.id, "node_name": node.name})
            except asyncio.QueueFull:
                pass

    return {
        "node": _as_node_dict(node),
        "auth_token": auth_token,
    }


@router.post("/agent/wireguard")
async def enroll_wireguard(
    req: WireguardEnrollRequest,
    x_node_token: Optional[str] = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    """A node sends its WireGuard public key and gets what it needs to join the node network."""
    if not x_node_token:
        raise HTTPException(401, "Missing node token")
    node = await get_node_by_token_or_401(x_node_token, db)
    return await hub.enroll(db, node, req.public_key)


@router.get("/agent/desired")
async def agent_desired(
    have: str = "",
    x_node_token: Optional[str] = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    """The instances this node should run. `have` is the hash of the list the agent already knows."""
    if not x_node_token:
        raise HTTPException(401, "Missing node token")
    node = await get_node_by_token_or_401(x_node_token, db)
    snap = await snapshot.snapshot(db, node.id)
    if have and have == snap["hash"]:
        return {"hash": snap["hash"], "unchanged": True}
    return snap


@router.post("/agent/observed")
async def agent_observed(
    req: ObservedRequest,
    x_node_token: Optional[str] = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    """What the node sees of its instances."""
    if not x_node_token:
        raise HTTPException(401, "Missing node token")
    node = await get_node_by_token_or_401(x_node_token, db)
    await observed.apply(node.id, {k: v.model_dump() for k, v in req.replicas.items()})
    return {"ok": True}


@router.get("/agent/commands")
async def get_pending_commands(
    limit: int = 20,
    x_node_token: Optional[str] = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    if not x_node_token:
        raise HTTPException(401, "Missing node token")
    node = await get_node_by_token_or_401(x_node_token, db)
    items = await _claim_pending_commands(db, node.id, limit=limit)
    return {"commands": items}


@router.post("/agent/commands/{command_id}/result")
async def report_command_result(
    command_id: int,
    req: CommandResultRequest,
    x_node_token: Optional[str] = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    if not x_node_token:
        raise HTTPException(401, "Missing node token")
    node = await get_node_by_token_or_401(x_node_token, db)
    await _apply_command_result(
        db,
        node=node,
        command_id=command_id,
        status=req.status,
        result_payload=req.result,
        error_message=req.error_message,
    )
    return {"ok": True}


@router.websocket("/ws/agent")
async def agent_ws(websocket: WebSocket):
    await websocket.accept()
    node: Optional[Node] = None
    node_id: Optional[int] = None
    client = websocket.client
    client_addr = f"{client.host}:{client.port}" if client else "unknown"

    log.info("WS agent connection accepted from %s", client_addr)

    try:
        hello = await asyncio.wait_for(websocket.receive_json(), timeout=10)
        if (hello or {}).get("type") != "auth":
            log.warning("WS agent from %s: first message was not auth, closing", client_addr)
            await websocket.send_json({"type": "error", "error": "First message must be auth"})
            await websocket.close(code=1008)
            return
        token = (hello or {}).get("token")
        if not token:
            log.warning("WS agent from %s: missing token, closing", client_addr)
            await websocket.send_json({"type": "error", "error": "Missing node token"})
            await websocket.close(code=1008)
            return

        async with AsyncSessionLocal() as db:
            node = await get_node_by_token_or_401(token, db)
            node.status = "online"
            node.last_seen = _utcnow()
            node.offline_since = None
            await db.commit()

        node_id = int(node.id)
        old_ws = _node_ws_connections.get(node_id)
        if old_ws is not None and old_ws is not websocket:
            try:
                await old_ws.close(code=1012)
            except Exception:
                pass
        _node_ws_connections[node_id] = websocket
        _get_node_event(node_id).set()

        # A new agent process cannot answer what the old one was doing; a mere reconnect can still
        session = (hello or {}).get("session")
        if session is None or _node_sessions.get(node_id) != session:
            await fail_commands_in_flight(node_id)
        _node_sessions[node_id] = session

        log.info("WS agent authenticated: node '%s' (id=%d) from %s", node.name, node_id, client_addr)

        panel_domain = _syscfg.get_panel_https_domain()
        await websocket.send_json({
            "type": "auth_ok",
            "node_id": node_id,
            "heartbeat_interval": node.heartbeat_interval,
            "server_time": _utcnow().isoformat(),
            # Agents registered over plain http move to this address themselves
            "panel_url": f"https://{panel_domain}" if panel_domain else None,
        })

        event = _get_node_event(node_id)
        receive_task: asyncio.Task = asyncio.create_task(websocket.receive_json())
        event_task: asyncio.Task = asyncio.create_task(event.wait())

        try:
            while True:
                done, _ = await asyncio.wait(
                    {receive_task, event_task},
                    timeout=3,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                # Another connection took over, or the panel dropped this one (a ping timed out): leave.
                # What that means for the node is decided below, once, in the finally.
                if _node_ws_connections.get(node_id) is not websocket:
                    break

                if event_task in done:
                    event.clear()
                    await _dispatch_queued_commands_ws(node_id, websocket)
                    await websocket.send_json({"type": "sync"})        # the list of what to run may have changed
                    event_task = asyncio.create_task(event.wait())

                if receive_task in done:
                    try:
                        incoming = receive_task.result()
                    except Exception:
                        break  # WebSocket closed
                    receive_task = asyncio.create_task(websocket.receive_json())

                    msg_type = (incoming or {}).get("type")
                    if msg_type == "heartbeat":
                        log.debug("WS heartbeat from node id=%d", node_id)
                        async with AsyncSessionLocal() as db:
                            live_node = await get_node_by_token_or_401(token, db)
                            live_node.status = "online"
                            live_node.last_seen = _utcnow()
                            live_node.offline_since = None
                            live_node.connection_type = "websocket"
                            caps = incoming.get("capabilities") or {}
                            if caps:
                                live_node.capabilities = json.dumps(caps)
                                if caps.get("agent_version"):
                                    live_node.agent_version = caps["agent_version"]
                            if incoming.get("metadata_json") is not None:
                                live_node.metadata_json = json.dumps(incoming.get("metadata_json") or {})
                            metrics = incoming.get("node_metrics") or {}
                            if metrics:
                                node_metrics.store_heartbeat(live_node, metrics)
                            committed = await _commit_with_sqlite_lock_retry(
                                db,
                                context=f"agent_ws/heartbeat node_id={node_id}",
                            )
                            if not committed:
                                log.debug("Skipped one heartbeat DB update due to SQLite lock: node_id=%d", node_id)
                        _push_node_event(node_id, {
                            "type": "node_health",
                            "cpu_percent": incoming.get("node_metrics", {}).get("cpu_percent"),
                            "memory_percent": incoming.get("node_metrics", {}).get("memory_percent"),
                            "disk_percent": incoming.get("node_metrics", {}).get("disk_percent"),
                            "connection_type": "websocket",
                        })
                    elif msg_type == "command_result":
                        command_id = int(incoming.get("command_id") or -1)
                        if command_id == -1:
                            pass  # streaming commands use id=-1, no DB record to update
                        else:
                            async with AsyncSessionLocal() as db:
                                live_node = await get_node_by_token_or_401(token, db)
                                await _apply_command_result(
                                    db,
                                    node=live_node,
                                    command_id=command_id,
                                    status=(incoming.get("status") or "failed"),
                                    result_payload=incoming.get("result"),
                                    error_message=incoming.get("error_message"),
                                )
                    elif msg_type == "stream_data":
                        stream_id = incoming.get("stream_id")
                        data = incoming.get("data")
                        if stream_id and data is not None:
                            push_node_stream_data(stream_id, data)
                    elif msg_type == "pong":
                        ping_id = incoming.get("ping_id", "")
                        fut = _pending_pings.get(ping_id)
                        if fut and not fut.done():
                            fut.set_result(True)
                    elif msg_type == "ping":
                        await websocket.send_json({"type": "pong", "server_time": _utcnow().isoformat()})
        finally:
            receive_task.cancel()
            event_task.cancel()

    except WebSocketDisconnect as e:
        log.info("WS agent disconnected: node id=%s code=%s", node_id, getattr(e, 'code', '?'))
    except Exception as e:
        log.exception("Unhandled error in agent_ws for node id=%s: %s", node_id, e)
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        should_mark_offline = False
        if node_id is not None:
            current_ws = _node_ws_connections.get(node_id)
            if current_ws is websocket:
                _node_ws_connections.pop(node_id, None)
                log.info("WS agent removed from active connections: node id=%d", node_id)
                should_mark_offline = True
            elif current_ws is None:
                should_mark_offline = True

        if node_id is not None and should_mark_offline:
            try:
                await node_gone(node_id)
            except Exception as e:
                log.error("Error marking node id=%s offline: %s", node_id, e)
