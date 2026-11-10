import asyncio
import json
import logging
from datetime import datetime
from typing import Optional
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from dbs import lifecycle as dbs_lifecycle
from core.database import AsyncSessionLocal
from core.models import DatabaseLink, Database, Application, Node, Instance
from nodes import commands as node_commands
from dbs.provision import _spec
from dbs.state import COMMAND_TIMEOUT_SECONDS, PROVISION_TIMEOUT_SECONDS, claim, release

log = logging.getLogger("cloudbase.databases")


async def _get_or_404(database_id: int, db: AsyncSession) -> Database:
    database = await db.get(Database, database_id)
    if database is None:
        raise HTTPException(404, "Database not found")
    return database


async def _node_for(database: Database, db: AsyncSession) -> Node:
    node = await db.get(Node, database.node_id)
    if node is None:
        raise HTTPException(409, "The node of this database no longer exists")
    return node


def _node_reachable(node: Node) -> bool:
    return node.is_local or (node.enabled and node.status == "online")


async def _run_on_node(node: Node, local_fn, command_type: str, payload: dict,
                       timeout: float = COMMAND_TIMEOUT_SECONDS) -> dict:
    """Run a db_manager call on the database's node and return its result."""
    if node.is_local:
        return await asyncio.to_thread(local_fn)
    if not _node_reachable(node):
        raise RuntimeError(f"Node '{node.name}' is offline")
    done = await node_commands.ask(node.id, command_type, payload, timeout=timeout)
    if done.status != "done":
        raise RuntimeError(done.error_message or f"{command_type} failed on node '{node.name}'")
    return done.result or {}


def _state_to_status(state: str) -> tuple[str, Optional[str]]:
    if state == "restarting":
        return "error", "The database container keeps restarting. Check the logs."
    return {"running": "running", "starting": "starting", "unhealthy": "unhealthy"}.get(state, "stopped"), None


async def _set_status(database_id: int, status: str, error: Optional[str] = None, **fields) -> None:
    async with AsyncSessionLocal() as db:
        database = await db.get(Database, database_id)
        if database is None:
            return
        database.status = status
        database.last_error = error
        for key, value in fields.items():
            setattr(database, key, value)
        if status == "running" and database.initialized_at is None:
            database.initialized_at = datetime.utcnow()
        await db.commit()


def _spawn(database_id: int, coro_fn) -> None:
    """Run an operation on a database the caller holds (claim()) in the background; it lets go when done."""

    async def runner():
        try:
            await coro_fn()
        except Exception as e:
            log.warning("[db] operation on database %s failed: %s", database_id, e)
            await _set_status(database_id, "error", str(e))
        finally:
            release(database_id)

    asyncio.create_task(runner())


async def _start_operation(database_id: int, op: str) -> None:
    """op: ensure | recreate | restart"""
    async with AsyncSessionLocal() as db:
        database = await db.get(Database, database_id)
        if database is None:
            return
        node = await _node_for(database, db)
        spec = _spec(database)
    fn = {"ensure": dbs_lifecycle.ensure_running, "recreate": dbs_lifecycle.recreate, "restart": dbs_lifecycle.restart}[op]
    timeout = COMMAND_TIMEOUT_SECONDS if op == "restart" else PROVISION_TIMEOUT_SECONDS
    result = await _run_on_node(node, lambda: fn(spec), f"db_{op}", {"spec": spec}, timeout=timeout)
    status, error = _state_to_status(result.get("state", "running"))
    await _set_status(database_id, status, error, container_id=result.get("container_id"))


async def _begin(database_id: int, db: AsyncSession, operation: str | None = None) -> tuple[Database, Node]:
    """Hold the database for an operation on its node, which must be reachable. Raises (and lets go) otherwise."""
    database = await _get_or_404(database_id, db)
    claim(database.id, operation)
    try:
        node = await _node_for(database, db)
        if not _node_reachable(node):
            raise HTTPException(409, f"Node '{node.name}' is offline")
    except BaseException:
        release(database.id)
        raise
    return database, node


async def _links_for(database_id: int, db: AsyncSession) -> list[dict]:
    result = await db.execute(
        select(DatabaseLink, Application)
        .join(Application, Application.id == DatabaseLink.app_id)
        .where(DatabaseLink.database_id == database_id)
        .order_by(Application.name)
    )
    rows = result.all()
    if not rows:
        return []
    app_ids = [app.id for _, app in rows]
    rep_result = await db.execute(
        select(Instance.app_id, Instance.node_id).where(Instance.app_id.in_(app_ids))
    )
    nodes_by_app: dict[int, set] = {}
    for app_id, node_id in rep_result.all():
        nodes_by_app.setdefault(app_id, set()).add(node_id)
    database = await db.get(Database, database_id)
    links = []
    for link, app in rows:
        app_nodes = nodes_by_app.get(app.id) or ({app.node_id} if app.cron_schedule and app.node_id else set())   # a cron job runs on its node
        links.append({
            "id": link.id,
            "app_id": app.id,
            "app_name": app.name,
            "env_var_name": link.env_var_name,
            # Phase 1: only instances on the database's node can reach it.
            "instances_elsewhere": any(n is not None and n != database.node_id for n in app_nodes),
        })
    return links


def _allowlist(database: Database) -> list[str]:
    try:
        return json.loads(database.public_allowlist or "[]")
    except ValueError:
        return []
