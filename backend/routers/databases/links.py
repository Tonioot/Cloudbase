from fastapi import HTTPException, APIRouter, Depends
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from core import auth as _auth
from dbs import engines as db_engines, links as db_links
from core.audit import log_audit
from core.database import get_db
from core.env_crypto import decrypt_env
from core.models import DatabaseLink, Application
from dbs.ops import _get_or_404
from dbs.provision import ENV_VAR_RE
from dbs.relay import push_proxies
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
    app_name = app.name  # read before a possible rollback expires the instance
    env_var = (req.env_var_name or db_engines.get_engine(database.engine).env_var).strip().upper()
    if not ENV_VAR_RE.match(env_var):
        raise HTTPException(400, "Env var name may only contain A–Z, 0–9 and _, and may not start with a digit")
    if env_var in decrypt_env(app.env_vars or ""):
        raise HTTPException(409, f"{app_name} already sets {env_var} itself. Remove it there or pick another name.")

    link = DatabaseLink(database_id=database.id, app_id=app.id, env_var_name=env_var)
    db.add(link)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, f"{app_name} already gets {env_var} from a linked database")
    await log_audit(db, "database.link", actor=actor, app_id=app.id, detail={
        "database_id": database.id, "name": database.name, "env_var": env_var,
    })
    await db.commit()
    await db_links.refresh(db)
    await push_proxies()
    return {"ok": True, "id": link.id, "env_var_name": env_var, "restart_required": True}


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
    await db.delete(link)
    await log_audit(db, "database.unlink", actor=actor, app_id=link.app_id, detail={
        "database_id": database_id, "env_var": link.env_var_name,
    })
    await db.commit()
    await db_links.refresh(db)
    await push_proxies()
    return {"ok": True, "restart_required": True}
