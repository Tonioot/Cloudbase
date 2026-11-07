import json
import asyncio
import logging
import secrets
from datetime import datetime, timedelta
from typing import Optional
from fastapi import HTTPException, WebSocket
from sqlalchemy import select, and_, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import AsyncSessionLocal
from core.models import Node, NodeCommand
from nodes.state import _get_command_event, _get_node_event, _node_ws_connections, _push_node_event

log = logging.getLogger("cloudbase.nodes")

INFLIGHT_REUSE_SECONDS = 30


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


async def _apply_command_result(
    db: AsyncSession,
    *,
    node: Node,
    command_id: int,
    status: str,
    result_payload: Optional[dict] = None,
    error_message: Optional[str] = None,
) -> None:
    """Store what a node answered to a command and wake whoever waits for it."""
    result = await db.execute(select(NodeCommand).where(NodeCommand.id == command_id))
    cmd = result.scalar_one_or_none()
    if not cmd or cmd.node_id != node.id:
        raise HTTPException(404, "Command not found")
    if status not in ("done", "failed"):
        raise HTTPException(400, "Status must be 'done' or 'failed'")

    cmd.status = status
    cmd.result = json.dumps(result_payload or {}) if result_payload is not None else None
    cmd.completed_at = _utcnow()
    cmd.error_message = error_message
    await _commit_with_sqlite_lock_retry(db, context=f"apply_command_result cmd_id={command_id}")
    _get_command_event(command_id).set()

    _push_node_event(node.id, {
        "type": "command_update",
        "command_id": command_id,
        "status": status,
        "app_id": cmd.app_id,
        "error_message": error_message,
    })


async def fail_commands_in_flight(node_id: int) -> None:
    """The node's agent started over (a new process): what it was working on will never be answered.

    (A cron job is not touched: it is reported after it finishes, and can outlive a restart.)"""
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(NodeCommand)
            .where(and_(NodeCommand.node_id == node_id, NodeCommand.status == "in_progress",
                        NodeCommand.command_type != "cron_run"))
            .values(status="failed", error_message="Connection to the node was lost", completed_at=_utcnow())
        )
        await db.commit()


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
                    # an old one is probably lost; reusing it would make every caller wait for nothing
                    NodeCommand.created_at > _utcnow() - timedelta(seconds=INFLIGHT_REUSE_SECONDS),
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
