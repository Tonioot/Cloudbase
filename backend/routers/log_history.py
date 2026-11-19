"""Searching an app's older output: what its containers printed, kept on each machine that ran them
(containers/output_log.py). The primary searches its own copy and asks every connected node at the same time."""
import asyncio
import time
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.serializers import _get_or_404
from containers import output_log
from core import auth, config as _cfg
from core.database import get_db
from core.models import Node
from nodes import commands as node_commands
from nodes.state import _node_ws_connections

router = APIRouter(tags=["logs"])

NODE_TIMEOUT_SECONDS = 20


@router.get("/api/apps/{app_id}/logs/history")
async def app_log_history(
    app_id: int,
    hours: float = Query(24, gt=0, le=24 * 366),
    before_ns: Optional[int] = Query(None, ge=0, description="only lines older than this (to load further back)"),
    q: str = Query("", max_length=200),
    errors: bool = False,
    instance_id: Optional[int] = Query(None, ge=1),
    limit: int = Query(500, ge=1, le=2000),
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(auth.require_permission("apps.view")),
):
    await _get_or_404(app_id, db)
    since_ns = int((time.time() - hours * 3600) * 1e9)
    query = {"app_id": app_id, "since_ns": since_ns, "until_ns": before_ns, "q": q, "errors_only": errors,
             "instance_id": instance_id, "limit": limit}

    nodes = (await db.execute(select(Node).where(Node.enabled == True))).scalars().all()  # noqa: E712
    local = next((n for n in nodes if n.is_local), None)
    remote = [n for n in nodes if not n.is_local]

    async def from_here() -> list[dict]:
        lines = await asyncio.to_thread(
            output_log.search, app_id, since_ns=since_ns, until_ns=before_ns, q=q, errors_only=errors,
            instance_id=instance_id, limit=limit)
        return [{**line, "node": local.name if local else "Primary"} for line in lines]

    async def from_node(node: Node) -> list[dict]:
        if node.id not in _node_ws_connections:
            raise RuntimeError("offline")
        cmd = await node_commands.ask(node.id, "logs_search", query, app_id=app_id, timeout=NODE_TIMEOUT_SECONDS)
        if cmd.status != "done":
            raise RuntimeError(cmd.error_message or "no answer")
        return [{**line, "node": node.name} for line in (cmd.result or {}).get("lines") or []]

    results = await asyncio.gather(from_here(), *(from_node(n) for n in remote), return_exceptions=True)
    entries: list[dict] = []
    missing: list[dict] = []
    for source, result in zip([local, *remote], results):
        if isinstance(result, BaseException):
            missing.append({"node": source.name if source else "Primary", "reason": _reason(result)})
        else:
            entries.extend(result)
    entries.sort(key=lambda e: e["ns"], reverse=True)
    page = entries[:limit]
    return {
        "entries": page,
        "has_more": len(entries) >= limit,
        "next_before_ns": page[-1]["ns"] if page else None,
        "missing": missing,
        "retention_days": _cfg.get_logs()["retention_days"],
    }


def _reason(error: BaseException) -> str:
    text = str(getattr(error, "detail", None) or error)
    if text == "offline":
        return "offline"
    if "Unsupported command" in text:
        return "runs an older Cloudbase; update it to search its logs"
    if "Timed out" in text:
        return "did not answer in time"
    return text[:200]
