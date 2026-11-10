from fastapi import WebSocket, WebSocketDisconnect, APIRouter
from core import auth
from containers import processes as pm
from core.database import AsyncSessionLocal
from core.models import Application
from apps.node_streams import stream_system_stats

router = APIRouter(tags=["stats"])



@router.websocket("/ws/apps/{app_id}/stats")
async def stream_stats(app_id: int, websocket: WebSocket):
    """The app's stats as the collector (workers/stats.py) makes them: one frame for all its instances, wherever they run.
    While this is open the app counts as watched, so it is sampled every 2 s."""
    if not await auth.authorize_websocket(websocket, "apps.view"):
        return
    await websocket.accept()
    async with AsyncSessionLocal() as db:
        if await db.get(Application, app_id) is None:
            await websocket.close(code=1008, reason="Unknown app")
            return
    q = pm.subscribe_stats(app_id)
    try:
        for point in pm.get_recent_stats(app_id):
            await websocket.send_json(point)
        while True:
            await websocket.send_json(await q.get())
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        pm.unsubscribe_stats(app_id, q)


@router.websocket("/ws/system/stats")
async def stream_system_stats_ws(websocket: WebSocket):
    if not await auth.authorize_websocket(websocket, "apps.view"):
        return
    await websocket.accept()
    try:
        await stream_system_stats(websocket.send_json)
    except (WebSocketDisconnect, Exception):
        pass
