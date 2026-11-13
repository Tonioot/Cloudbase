from fastapi import HTTPException, APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from core import auth as _auth
from core.database import get_db
from core.models import DatabaseLink, Application
from dbs import link_ops
from dbs.ops import _get_or_404
from routers.databases.schemas import LinkRequest

router = APIRouter(prefix="/api/databases", tags=["databases"])


@router.post("/{database_id}/links")
async def create_link(
    database_id: int,
    req: LinkRequest,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("databases.manage")),
    actor: str = Depends(_auth.get_current_actor),
):
    database = await _get_or_404(database_id, db)
    app = await db.get(Application, req.app_id)
    if app is None:
        raise HTTPException(404, "App not found")
    link = await link_ops.add_link(db, database, app, req.env_var_name, actor, replace_env=bool(req.replace_env))
    return {"ok": True, "id": link.id, "env_var_name": link.env_var_name, "restart_required": True}


@router.delete("/{database_id}/links/{link_id}")
async def delete_link(
    database_id: int,
    link_id: int,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("databases.manage")),
    actor: str = Depends(_auth.get_current_actor),
):
    link = await db.get(DatabaseLink, link_id)
    if link is None or link.database_id != database_id:
        raise HTTPException(404, "Link not found")
    await link_ops.remove_link(db, link, actor)
    return {"ok": True, "restart_required": True}
