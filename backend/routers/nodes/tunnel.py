import logging
from fastapi import APIRouter, WebSocket, HTTPException, WebSocketDisconnect
from sqlalchemy import select
from core.database import AsyncSessionLocal
from nodes.commands import _utcnow
from nodes.recovery import _regen_nginx_for_app
from nodes.registry import get_node_by_token_or_401
import tunnel_server
from core.models import ApplicationReplica

log = logging.getLogger("cloudbase.nodes")


router = APIRouter(prefix="/api/nodes", tags=["nodes"])


@router.websocket("/ws/tunnel/{replica_id}")
async def replica_tunnel_ws(replica_id: int, websocket: WebSocket):
    """Reverse-tunnel endpoint — the node agent connects here to expose its
    replica container on a localhost port on the main node.  Nginx then
    load-balances to 127.0.0.1:{tunnel_port} instead of the remote node's
    public IP, eliminating the need for open inbound firewall ports.
    """

    # Authenticate: accept token from header OR query param (websocket libraries
    # cannot always set custom headers — query param is the fallback).
    token = (
        websocket.headers.get("x-node-token")
        or websocket.query_params.get("token")
    )
    if not token:
        await websocket.close(code=1008, reason="Missing node token")
        return

    async with AsyncSessionLocal() as db:
        try:
            node = await get_node_by_token_or_401(token, db)
        except HTTPException:
            await websocket.close(code=1008, reason="Invalid node token")
            return

        # Verify the replica belongs to this node
        rep_result = await db.execute(
            select(ApplicationReplica).where(ApplicationReplica.id == replica_id)
        )
        replica = rep_result.scalar_one_or_none()
        if not replica or replica.node_id != node.id:
            await websocket.close(code=1008, reason="Replica not found or not owned by this node")
            return
        app_id = replica.app_id

    await websocket.accept()
    log.info("[tunnel-ws] replica=%d node=%s connected", replica_id, node.name)

    # Allocate a free localhost port and start the TCP listener
    local_port = await tunnel_server.open_tunnel(replica_id, websocket)
    if local_port is None:
        await websocket.close(code=1011, reason="No tunnel ports available")
        return

    # Persist tunnel_port and promote status → running
    async with AsyncSessionLocal() as db:
        rep_result = await db.execute(
            select(ApplicationReplica).where(ApplicationReplica.id == replica_id)
        )
        replica = rep_result.scalar_one_or_none()
        if replica:
            replica.tunnel_port = local_port
            replica.status = "running"
            replica.last_error = None
            replica.updated_at = _utcnow()
            await db.commit()

    log.info("[tunnel-ws] replica=%d allocated 127.0.0.1:%d", replica_id, local_port)

    # Regenerate nginx to include the new backend
    await _regen_nginx_for_app(app_id)

    # Relay messages until the agent disconnects
    try:
        async for message in websocket.iter_text():
            await tunnel_server.dispatch_message(replica_id, message)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("[tunnel-ws] replica=%d error: %s", replica_id, e)
    finally:
        log.info("[tunnel-ws] replica=%d disconnected, tearing down tunnel", replica_id)

        # Guard against race condition: if the agent reconnected and opened a new
        # tunnel for this replica before our cleanup runs, leave the new tunnel alone.
        was_active = tunnel_server.is_active_tunnel(replica_id, websocket)
        if was_active:
            await tunnel_server.close_tunnel(replica_id)

        # Only update DB state if we were still the active tunnel
        if was_active:
            async with AsyncSessionLocal() as db:
                rep_result = await db.execute(
                    select(ApplicationReplica).where(ApplicationReplica.id == replica_id)
                )
                replica = rep_result.scalar_one_or_none()
                if replica:
                    was_running = replica.status == "running"
                    replica.tunnel_port = None
                    # An intentional stop sets "stopping" before the agent closes the
                    # tunnel. Any other drop (agent restart, network blip, crash) is
                    # handed to recovery: start_replica on the agent reattaches the
                    # tunnel if the container still runs, or restarts it if it doesn't.
                    # Judging by "agent WS still connected" raced with agent restarts
                    # and left running containers marked stopped without a tunnel.
                    if replica.status == "stopping":
                        replica.status = "stopped"
                    elif replica.status in ("running", "starting"):
                        replica.status = "node_offline"
                        replica.substatus = None
                    replica.updated_at = _utcnow()
                    await db.commit()
                    if was_running:
                        # Regenerate nginx to remove the dropped backend
                        await _regen_nginx_for_app(app_id)
