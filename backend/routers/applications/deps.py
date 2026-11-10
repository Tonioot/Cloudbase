from fastapi import Request, HTTPException, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import get_db
from core.models import Node, Instance
from core import auth as _auth

async def _request_can_manage_apps(request: Request) -> bool:
    """Return True if the requester has apps.configure (or is superadmin)."""
    user = await _auth.request_user(request)
    return bool(user) and _auth.has_permission(user, "apps.configure")


async def _source_archive_auth(request: Request, app_id: int, db: AsyncSession = Depends(get_db)):
    # Only the agent of a node that runs this app may fetch its source.
    await _require_node_agent_for_app()(request, app_id, db)


def _require_node_agent_for_app(app_id_param: str = "app_id"):
    """Dependency factory that authenticates node-agent calls and verifies the
    calling node actually has a instance for the requested app.

    Needs the X-Node-Token of a node that has a instance of app_id.
    """
    async def _dep(request: Request, app_id: int, db: AsyncSession = Depends(get_db)):
        node_token = request.headers.get("X-Node-Token", "")
        if not node_token:
            raise HTTPException(status_code=401, detail="Node agent token required")

        node_result = await db.execute(
            select(Node).where(Node.auth_token == node_token, Node.enabled == True)
        )
        node = node_result.scalar_one_or_none()
        if node is None:
            raise HTTPException(status_code=401, detail="Invalid or disabled node token")

        # "At least one": a node may run several instances of the same app
        instance_result = await db.execute(
            select(Instance.id).where(
                Instance.app_id == app_id,
                Instance.node_id == node.id,
            ).limit(1)
        )
        if instance_result.first() is None:
            raise HTTPException(status_code=403, detail="Node has no instance for this app")

    return _dep
