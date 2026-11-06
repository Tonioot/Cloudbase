import json
import asyncio
import logging
import secrets
from datetime import datetime
from typing import Optional
from fastapi import HTTPException, WebSocket
from sqlalchemy import select, and_, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import AsyncSessionLocal
from core.models import Node, NodeCommand
from nodes.state import _get_command_event, _get_node_event, _node_ws_connections, _push_node_event
from core.models import Application
from core.models import ApplicationReplica

log = logging.getLogger("cloudbase.nodes")


async def _claim_pending_commands(db: AsyncSession, node_id: int, limit: int = 100) -> list[dict]:
    capped_limit = min(max(limit, 1), 100)
    result = await db.execute(
        select(NodeCommand)
        .where(and_(NodeCommand.node_id == node_id, NodeCommand.status == "queued"))
        .order_by(NodeCommand.created_at.asc())
        .limit(capped_limit)
    )
    commands = result.scalars().all()
    now = _utcnow()
    items = []
    for cmd in commands:
        claim_res = await db.execute(
            update(NodeCommand)
            .where(and_(NodeCommand.id == cmd.id, NodeCommand.status == "queued"))
            .values(status="in_progress", dispatched_at=now)
        )
        if claim_res.rowcount != 1:
            # Another concurrent claimer already took this command.
            continue
        items.append(
            {
                "id": cmd.id,
                "app_id": cmd.app_id,
                "command_type": cmd.command_type,
                "payload": json.loads(cmd.payload or "{}"),
                "idempotency_key": cmd.idempotency_key,
                "created_at": cmd.created_at.isoformat() if cmd.created_at else None,
            }
        )
    await db.commit()
    return items


# Called with the replica id when a node reports an instance started (nodes/promote.py registers here;
# commands.py cannot import it, since promote builds on commands).
_replica_started_hooks: list = []


def on_replica_started(hook) -> None:
    _replica_started_hooks.append(hook)


async def _apply_command_result(
    db: AsyncSession,
    *,
    node: Node,
    command_id: int,
    status: str,
    result_payload: Optional[dict] = None,
    error_message: Optional[str] = None,
) -> None:
    result = await db.execute(select(NodeCommand).where(NodeCommand.id == command_id))
    cmd = result.scalar_one_or_none()
    if not cmd or cmd.node_id != node.id:
        raise HTTPException(404, "Command not found")

    if status not in ("done", "failed"):
        raise HTTPException(400, "Status must be 'done' or 'failed'")

    started_replica_id = None
    cmd.status = status
    cmd.result = json.dumps(result_payload or {}) if result_payload is not None else None
    cmd.completed_at = _utcnow()
    cmd.error_message = error_message
    try:
        payload = json.loads(cmd.payload or "{}")
    except Exception:
        payload = {}

    if cmd.app_id:

        app_result = await db.execute(select(Application).where(Application.id == cmd.app_id))
        app = app_result.scalar_one_or_none()
        if app:
            if cmd.command_type == "deploy_app":
                if status == "done":
                    if result_payload:
                        app.working_dir = result_payload.get("working_dir", app.working_dir)
                        if "nginx_enabled" in result_payload:
                            app.nginx_enabled = bool(result_payload.get("nginx_enabled"))
                        # Use the actual status from the agent (auto_start may have started it)
                        remote_status = result_payload.get("status")
                        if remote_status in ("running", "stopped", "error"):
                            app.status = remote_status
                        else:
                            app.status = "stopped"
                    else:
                        app.status = "stopped"
                    app.last_error = None
                else:
                    app.status = "error"
                    if error_message:
                        app.last_error = error_message
                # Update the first ApplicationReplica status if it was created pre-deploy
                first_replica_id = payload.get("first_replica_id")
                if first_replica_id:
                    frep_result = await db.execute(
                        select(ApplicationReplica).where(ApplicationReplica.id == int(first_replica_id))
                    )
                    first_replica = frep_result.scalar_one_or_none()
                    if first_replica:
                        if status == "done":
                            deployed_status = (result_payload or {}).get("status", "stopped")
                            first_replica.status = deployed_status if deployed_status in ("running", "stopped", "error") else "stopped"
                            first_replica.last_error = None
                        else:
                            first_replica.status = "error"
                            if error_message:
                                first_replica.last_error = error_message
            elif cmd.command_type == "update_app":
                if status == "done":
                    if result_payload and "nginx_enabled" in result_payload:
                        app.nginx_enabled = bool(result_payload.get("nginx_enabled"))
                    app.last_error = None
                else:
                    if error_message:
                        app.last_error = error_message
            elif cmd.command_type == "git_pull":
                if status == "done":
                    app.last_error = None
                elif error_message:
                    app.last_error = error_message
            elif cmd.command_type in ("start_app", "stop_app", "restart_app"):
                if status == "done":
                    action = cmd.command_type.replace("_app", "")
                    reported_status = (result_payload or {}).get("status")
                    if action == "start":
                        if reported_status in ("running", "starting", "stopped", "error"):
                            app.status = reported_status
                        else:
                            app.status = "running"
                    elif action == "restart":
                        if reported_status in ("running", "restarting", "stopped", "error"):
                            app.status = reported_status
                        else:
                            app.status = "running"
                    else:
                        if reported_status in ("stopped", "stopping", "running", "error"):
                            app.status = reported_status
                        else:
                            app.status = "stopped"
                    app.last_error = None
                else:
                    app.status = "error"
                    if error_message:
                        app.last_error = error_message
            elif cmd.command_type == "delete_app":
                if status == "done":
                    app.status = "stopped"
                    app.last_error = None
                else:
                    app.status = "error"
                    if error_message:
                        app.last_error = error_message
            elif cmd.command_type == "save_nginx_config":
                if status == "done":
                    if (result_payload or {}).get("ok"):
                        app.nginx_enabled = True
                        app.last_error = None
                    else:
                        app.last_error = (result_payload or {}).get("message")
                elif error_message:
                    app.last_error = error_message
            elif cmd.command_type == "save_maintenance_pages":
                if status == "done":
                    if (result_payload or {}).get("ok", True):
                        app.last_error = None
                    else:
                        app.last_error = (result_payload or {}).get("message")
                elif error_message:
                    app.last_error = error_message
            elif cmd.command_type == "toggle_maintenance_mode":
                if status == "done":
                    app.last_error = None
                else:
                    if "previous_maintenance_mode" in payload:
                        app.maintenance_mode = bool(payload.get("previous_maintenance_mode"))
                    if "previous_update_mode" in payload:
                        app.update_mode = bool(payload.get("previous_update_mode"))
                    if error_message:
                        app.last_error = error_message
            elif cmd.command_type == "toggle_update_mode":
                if status == "done":
                    app.last_error = None
                else:
                    if "previous_maintenance_mode" in payload:
                        app.maintenance_mode = bool(payload.get("previous_maintenance_mode"))
                    if "previous_update_mode" in payload:
                        app.update_mode = bool(payload.get("previous_update_mode"))
                    if error_message:
                        app.last_error = error_message
            elif cmd.command_type in ("start_replica", "stop_replica"):
                replica_id = payload.get("replica_id")
                if replica_id:
                    rep_result = await db.execute(
                        select(ApplicationReplica).where(ApplicationReplica.id == int(replica_id))
                    )
                    replica = rep_result.scalar_one_or_none()
                    if replica:
                        if cmd.command_type == "start_replica":
                            if status == "done":
                                # Container is up on the remote node; stay in "starting"
                                # until the primary can reach it (nodes/promote.py sets "running").
                                replica.status = "starting"
                                started_replica_id = replica.id
                                # Keep the known id when the agent reused a container without reporting one
                                replica.container_id = (result_payload or {}).get("container_id") or replica.container_id
                                replica.last_error = None
                            else:
                                replica.status = "error"
                                replica.last_error = error_message
                        else:  # stop_replica
                            replica.status = "stopped" if status == "done" else "error"
                            if status != "done" and error_message:
                                replica.last_error = error_message
            app.updated_at = _utcnow()

    await _commit_with_sqlite_lock_retry(db, context=f"apply_command_result cmd_id={command_id}")
    _get_command_event(command_id).set()
    if started_replica_id is not None:
        for hook in _replica_started_hooks:
            hook(started_replica_id)

    # Broadcast completion event to browser subscribers — use a fresh session to avoid
    # autoflush on the already-committed session triggering a lock error.
    new_app_status = None
    if cmd.app_id:
        async with AsyncSessionLocal() as fresh_db:
            app_result2 = await fresh_db.execute(select(Application).where(Application.id == cmd.app_id))
            app2 = app_result2.scalar_one_or_none()
            if app2:
                new_app_status = app2.status
    _push_node_event(node.id, {
        "type": "command_update",
        "command_id": command_id,
        "status": status,
        "app_id": cmd.app_id,
        "app_status": new_app_status,
        "error_message": error_message,
    })


def _utcnow() -> datetime:
    return datetime.utcnow()


async def _commit_with_sqlite_lock_retry(
    db: AsyncSession,
    *,
    context: str,
    attempts: int = 4,
    base_delay_seconds: float = 0.05,
) -> bool:
    """Commit with short retry/backoff for transient SQLite lock contention."""
    for attempt in range(1, attempts + 1):
        try:
            await db.commit()
            return True
        except OperationalError as exc:
            msg = str(exc).lower()
            if "database is locked" not in msg:
                await db.rollback()
                raise
            await db.rollback()
            if attempt >= attempts:
                log.warning("SQLite lock persisted after %d attempts (%s)", attempts, context)
                return False
            await asyncio.sleep(base_delay_seconds * attempt)


async def queue_node_command(
    db: AsyncSession,
    *,
    node_id: int,
    command_type: str,
    payload: dict,
    app_id: Optional[int] = None,
    allow_existing_inflight: bool = False,
) -> NodeCommand:
    if allow_existing_inflight:
        existing_res = await db.execute(
            select(NodeCommand)
            .where(
                and_(
                    NodeCommand.node_id == node_id,
                    NodeCommand.command_type == command_type,
                    NodeCommand.status.in_(["queued", "in_progress"]),
                    NodeCommand.app_id == app_id,
                )
            )
            .order_by(NodeCommand.created_at.desc())
            .limit(1)
        )
        existing = existing_res.scalar_one_or_none()
        if existing is not None:
            log.info(
                "queue_command dedupe: using existing id=%d type=%s node_id=%d app_id=%s status=%s",
                existing.id,
                command_type,
                node_id,
                app_id,
                existing.status,
            )
            return existing

    cmd = NodeCommand(
        node_id=node_id,
        app_id=app_id,
        command_type=command_type,
        payload=json.dumps(payload or {}),
        status="queued",
        idempotency_key=secrets.token_hex(16),
    )
    db.add(cmd)
    await db.commit()
    await db.refresh(cmd)
    ws_connected = node_id in _node_ws_connections
    log.info("queue_command: id=%d type=%s node_id=%d ws_connected=%s",
             cmd.id, command_type, node_id, ws_connected)
    _get_node_event(node_id).set()
    return cmd


async def wait_for_node_command(
    _db: AsyncSession,
    command_id: int,
    *,
    timeout_seconds: float = 20.0,
    poll_seconds: float = 0.5,
) -> NodeCommand:
    # Each poll uses a fresh session so SQLite's snapshot isolation doesn't hide
    # status updates committed by _claim_pending_commands / _apply_command_result.
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    event = _get_command_event(command_id)
    log.debug("wait_for_command: id=%d timeout=%.0fs", command_id, timeout_seconds)
    cmd = None
    while asyncio.get_running_loop().time() < deadline:
        async with AsyncSessionLocal() as fresh_db:
            result = await fresh_db.execute(select(NodeCommand).where(NodeCommand.id == command_id))
            cmd = result.scalar_one_or_none()
        if not cmd:
            raise HTTPException(404, "Node command not found")
        if cmd.status in ("done", "failed"):
            log.debug("wait_for_command: id=%d completed with status=%s", command_id, cmd.status)
            return cmd
        remaining = max(deadline - asyncio.get_running_loop().time(), 0)
        wait_for = min(poll_seconds, remaining)
        if wait_for <= 0:
            break
        try:
            await asyncio.wait_for(event.wait(), timeout=wait_for)
        except asyncio.TimeoutError:
            pass
        finally:
            event.clear()
    log.warning("wait_for_command: id=%d TIMED OUT after %.0fs (status still %s)",
                command_id, timeout_seconds, cmd.status if cmd else '?')
    raise HTTPException(504, "Timed out waiting for node command result")


async def _dispatch_queued_commands_ws(node_id: int, websocket: WebSocket, *, limit: int = 100) -> int:
    async with AsyncSessionLocal() as db:
        items = await _claim_pending_commands(db, node_id, limit=limit)
    if items:
        log.info("dispatch %d command(s) to node id=%d: %s",
                 len(items), node_id, [i["command_type"] for i in items])
    for item in items:
        await websocket.send_json({"type": "command", "command": item})
    return len(items)
