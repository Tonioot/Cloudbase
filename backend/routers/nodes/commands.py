import asyncio
from fastapi import APIRouter, WebSocketDisconnect, Depends, WebSocket
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import get_db
from nodes import commands as node_commands
from core import auth as _auth
from nodes.registry import get_node_or_404
from nodes.state import _subscribe_node_events, _unsubscribe_node_events

router = APIRouter(prefix="/api/nodes", tags=["nodes"])


@router.get("/{node_id}/commands")
async def list_node_commands(node_id: int, db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("nodes.view"))):
    """The node's recent commands (since the panel started), newest first."""
    await get_node_or_404(node_id, db)
    return node_commands.history(node_id)


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
