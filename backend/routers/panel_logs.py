import asyncio
import json
import logging
from typing import Optional
from fastapi import HTTPException, Depends, APIRouter, WebSocket as _WS, WebSocketDisconnect as _WSD
from core import auth, system_logs

log = logging.getLogger("cloudbase.main")


router = APIRouter()


# /value endpoint intentionally omitted — raw tokens are resolved server-side only.


# ── System logs ──────────────────────────────────────────────────────────────
@router.get("/api/system/log-sources")
async def get_log_sources(_: dict = Depends(auth.require_permission("system.manage"))):
    return {"sources": system_logs.source_info()}


@router.get("/api/system/logs")
async def get_system_logs(
    source: str = "server",
    limit: int = 500,
    component: Optional[str] = None,
    level: Optional[str] = None,
    q: Optional[str] = None,
    _: dict = Depends(auth.require_permission("system.manage")),
):
    if source not in system_logs.SOURCES:
        raise HTTPException(404, f"Unknown log source '{source}'")
    try:
        return await asyncio.to_thread(system_logs.read_entries, source, limit, component, level, q)
    except PermissionError:
        raise HTTPException(403, f"Cloudbase can't read {system_logs.SOURCES[source][1]} (file permissions)")


@router.websocket("/ws/system/logs")
async def stream_system_logs(websocket: _WS, source: str = "server"):
    """Live tail of a log source; sends JSON batches of entries ({entries: [...]})."""
    if not await auth.authorize_websocket(websocket, "system.manage"):
        return
    if source not in system_logs.SOURCES:
        await websocket.close(code=4404)
        return
    await websocket.accept()

    async def _pump():
        try:
            async for batch in system_logs.tail(source):
                if batch:
                    await websocket.send_text(json.dumps({"entries": batch}))
        except (_WSD, asyncio.CancelledError):
            pass
        except Exception as e:
            log.warning("system log stream (%s) ended: %s", source, e)

    # A quiet log never sends, so watch for the disconnect separately —
    # otherwise the tail would keep polling after the tab is closed.
    pump = asyncio.create_task(_pump())
    try:
        while not pump.done():
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break
    except Exception:
        pass
    finally:
        pump.cancel()
