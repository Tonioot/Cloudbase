import json
import asyncio
import secrets
from fastapi import APIRouter, HTTPException, WebSocketDisconnect, Depends, WebSocket
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import get_db
from core.models import NodeCommand
from core import auth as _auth
from nodes.registry import get_node_or_404
from nodes.state import _node_stats_history, _node_ws_connections, _record_node_stats, _subscribe_node_events, _unsubscribe_node_events, subscribe_node_stream, unsubscribe_node_stream

router = APIRouter(prefix="/api/nodes", tags=["nodes"])


@router.get("/{node_id}/commands")
async def list_node_commands(node_id: int, db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("nodes.view"))):
    await get_node_or_404(node_id, db)
    result = await db.execute(
        select(NodeCommand)
        .where(NodeCommand.node_id == node_id)
        .order_by(NodeCommand.created_at.desc())
        .limit(200)
    )
    commands = result.scalars().all()
    return [
        {
            "id": c.id,
            "node_id": c.node_id,
            "app_id": c.app_id,
            "command_type": c.command_type,
            "payload": json.loads(c.payload or "{}"),
            "status": c.status,
            "idempotency_key": c.idempotency_key,
            "result": json.loads(c.result or "{}") if c.result else None,
            "error_message": c.error_message,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
            "dispatched_at": c.dispatched_at.isoformat() if c.dispatched_at else None,
            "completed_at": c.completed_at.isoformat() if c.completed_at else None,
        }
        for c in commands
    ]


@router.get("/{node_id}/commands/{command_id}")
async def get_node_command_status(node_id: int, command_id: int, db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("nodes.view"))):
    await get_node_or_404(node_id, db)
    result = await db.execute(
        select(NodeCommand).where(NodeCommand.node_id == node_id, NodeCommand.id == command_id)
    )
    c = result.scalar_one_or_none()
    if not c:
        raise HTTPException(404, "Command not found")
    return {
        "id": c.id,
        "status": c.status,
        "result": json.loads(c.result or "{}") if c.result else None,
        "error_message": c.error_message,
        "completed_at": c.completed_at.isoformat() if c.completed_at else None,
    }


@router.websocket("/{node_id}/events")
async def node_events_ws(node_id: int, websocket: WebSocket):
    """Browser-facing WebSocket: streams command updates and node health events."""
    if not await _auth.authorize_websocket(websocket, "nodes.view"):
        return
    await websocket.accept()
    q = _subscribe_node_events(node_id)
    try:
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=15.0)
                await websocket.send_json(event)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "ping"})
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        _unsubscribe_node_events(node_id, q)


@router.websocket("/{node_id}/stats")
async def node_stats_ws(node_id: int, websocket: WebSocket):
    """Browser-facing WebSocket: streams the remote node's system stats."""
    if not await _auth.authorize_websocket(websocket, "apps.view"):
        return
    await websocket.accept()
    agent_ws_conn = _node_ws_connections.get(node_id)
    if not agent_ws_conn:
        await websocket.send_json({"error": "Node not connected via WebSocket"})
        await websocket.close(code=1011)
        return

    # Replay buffered history so the chart populates immediately
    for snapshot in list(_node_stats_history.get(node_id, [])):
        try:
            await websocket.send_text(snapshot)
        except Exception:
            return

    stream_id = secrets.token_hex(8)
    q = subscribe_node_stream(stream_id)
    try:
        await agent_ws_conn.send_json({
            "type": "command",
            "command": {
                "id": -1,
                "command_type": "node_stats_stream",
                "payload": {"stream_id": stream_id},
            },
        })
        while True:
            try:
                data = await asyncio.wait_for(q.get(), timeout=30.0)
                _record_node_stats(node_id, data)
                await websocket.send_text(data)
            except asyncio.TimeoutError:
                pass
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        unsubscribe_node_stream(stream_id, q)
        try:
            if _node_ws_connections.get(node_id) is agent_ws_conn:
                await agent_ws_conn.send_json({"type": "cancel_stream", "stream_id": stream_id})
        except Exception:
            pass


@router.websocket("/{node_id}/commands/live")
async def node_commands_live_ws(node_id: int, websocket: WebSocket, db: AsyncSession = Depends(get_db)):
    """Browser-facing WebSocket: streams live command queue updates."""
    if not await _auth.authorize_websocket(websocket, "nodes.view"):
        return
    await websocket.accept()

    # Send initial snapshot
    result = await db.execute(
        select(NodeCommand)
        .where(and_(NodeCommand.node_id == node_id, NodeCommand.status.in_(["queued", "in_progress"])))
        .order_by(NodeCommand.created_at.asc())
        .limit(50)
    )
    active_cmds = result.scalars().all()
    await websocket.send_json({
        "type": "snapshot",
        "commands": [
            {
                "id": c.id,
                "command_type": c.command_type,
                "app_id": c.app_id,
                "status": c.status,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in active_cmds
        ],
    })

    q = _subscribe_node_events(node_id)
    try:
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=15.0)
                if event.get("type") == "command_update":
                    await websocket.send_json({"type": "command_updated", "command": event})
                elif event.get("type") == "ping":
                    await websocket.send_json({"type": "ping"})
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "ping"})
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        _unsubscribe_node_events(node_id, q)
