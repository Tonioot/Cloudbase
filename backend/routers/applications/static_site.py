"""A node hands in the static site it built (apps/static_sites.py)."""
from typing import Optional

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from apps import static_sites
from core.database import get_db
from nodes.registry import get_node_by_token_or_401

agent_router = APIRouter(prefix="/api/nodes/agent/apps", tags=["applications"])


@agent_router.post("/{app_id}/static-site/{build_id}")
async def agent_upload_static_site(
    app_id: int,
    build_id: str,
    request: Request,
    x_node_token: Optional[str] = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    """Only the node that was asked to build this site, once, for the build it was given."""
    node = await get_node_by_token_or_401(x_node_token or "", db)
    size = await static_sites.receive_upload(build_id, app_id, node.id, request.stream())
    return {"ok": True, "size_bytes": size}
