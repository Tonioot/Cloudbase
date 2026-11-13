"""The Connections tab of an app: everything it is linked to (databases and other apps) in one place, and what is
linked to it. A connection owns its environment variable: the app cannot set that name itself (see update.py)."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from apps import app_links
from apps.serializers import _get_or_404
from core import auth as _auth
from core.audit import log_audit
from core.database import get_db
from core.env_crypto import decrypt_env, encrypt_env
from core.models import AppLink, Application, Database, DatabaseLink
from dbs import engines as db_engines, link_ops

router = APIRouter(prefix="/api/apps", tags=["applications"])


class ConnectionRequest(BaseModel):
    kind: str = Field(pattern="^(database|app)$")
    target_id: int
    env_var_name: str | None = Field(default=None, max_length=100)
    replace_env: bool = False       # the app sets that name itself: let the connection take its place


@router.get("/{app_id}/connections")
async def list_connections(app_id: int, db: AsyncSession = Depends(get_db),
                           _user: dict = Depends(_auth.require_permission("apps.view"))):
    await _get_or_404(app_id, db)
    databases = (await db.execute(
        select(DatabaseLink, Database).join(Database, Database.id == DatabaseLink.database_id)
        .where(DatabaseLink.app_id == app_id).order_by(DatabaseLink.id))).all()
    outgoing = (await db.execute(
        select(AppLink, Application).join(Application, Application.id == AppLink.target_app_id)
        .where(AppLink.app_id == app_id).order_by(AppLink.id))).all()
    incoming = (await db.execute(
        select(AppLink, Application).join(Application, Application.id == AppLink.app_id)
        .where(AppLink.target_app_id == app_id).order_by(AppLink.id))).all()
    return {
        "databases": [{"id": l.id, "target_id": d.id, "name": d.name, "engine": d.engine, "status": d.status,
                       "env_var_name": l.env_var_name} for l, d in databases],
        "apps": [{"id": l.id, "target_id": a.id, "name": a.name, "env_var_name": l.env_var_name} for l, a in outgoing],
        "used_by": [{"id": l.id, "app_id": a.id, "name": a.name} for l, a in incoming],
        "reserved_env": sorted(await app_links.reserved_env_names(db, app_id)),
    }


@router.get("/{app_id}/connections/options")
async def connection_options(app_id: int, db: AsyncSession = Depends(get_db),
                             _user: dict = Depends(_auth.require_permission("apps.view"))):
    """What this app can still be connected to, with the variable name each would get."""
    await _get_or_404(app_id, db)
    taken_dbs = set((await db.execute(select(DatabaseLink.database_id).where(DatabaseLink.app_id == app_id))).scalars())
    taken_apps = set((await db.execute(select(AppLink.target_app_id).where(AppLink.app_id == app_id))).scalars())
    databases = (await db.execute(select(Database).order_by(Database.name))).scalars().all()
    apps = (await db.execute(select(Application).order_by(Application.name))).scalars().all()
    return {
        "databases": [{"id": d.id, "name": d.name, "engine": d.engine, "default_env_var": db_engines.get_engine(d.engine).env_var}
                      for d in databases if d.id not in taken_dbs],
        "apps": [{"id": a.id, "name": a.name, "default_env_var": app_links.default_env_var(a)}
                 for a in apps if a.id != app_id and a.id not in taken_apps and not a.no_web and not a.cron_schedule],
    }


@router.post("/{app_id}/connections")
async def add_connection(app_id: int, req: ConnectionRequest, db: AsyncSession = Depends(get_db),
                         _user: dict = Depends(_auth.require_permission("apps.configure")),
                         actor: str = Depends(_auth.get_current_actor)):
    app = await _get_or_404(app_id, db)
    if req.kind == "database":
        database = await db.get(Database, req.target_id)
        if database is None:
            raise HTTPException(404, "Database not found")
        link = await link_ops.add_link(db, database, app, req.env_var_name, actor, replace_env=req.replace_env)
        return {"ok": True, "id": link.id, "env_var_name": link.env_var_name, "restart": [app_id]}

    target = await db.get(Application, req.target_id)
    if target is None:
        raise HTTPException(404, "App not found")
    if app.cron_schedule:
        raise HTTPException(400, "A cron job cannot be connected to an app: its runs do not get the address")
    if target.id == app.id:
        raise HTTPException(400, "An app cannot be connected to itself")
    if target.no_web or target.cron_schedule:
        raise HTTPException(400, f"{target.name} has no web server, so there is nothing to connect to")
    app_name = app.name
    env_var = (req.env_var_name or app_links.default_env_var(target)).strip().upper()
    if not app_links.ENV_VAR_RE.match(env_var):
        raise HTTPException(400, "Env var name may only contain A–Z, 0–9 and _, and may not start with a digit")
    own = decrypt_env(app.env_vars or "")
    if env_var in own and not req.replace_env:
        raise HTTPException(409, f"{app_name} already sets {env_var} itself. Replace it with the connection, or pick another name.")
    if env_var in await app_links.reserved_env_names(db, app.id):
        raise HTTPException(409, f"{app_name} already gets {env_var} from another connection")
    if env_var in own:
        del own[env_var]
        app.env_vars = encrypt_env(own)
    target_name = target.name
    link = AppLink(app_id=app.id, target_app_id=target.id, env_var_name=env_var)
    db.add(link)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, f"{app_name} is already connected to {target_name}")
    await log_audit(db, "app.connect", actor=actor, app_id=app.id, detail={"target_app_id": target.id, "name": target_name, "env_var": env_var})
    await db.commit()
    # Both start again to apply it: the app gets the address, the target joins the network they share
    return {"ok": True, "id": link.id, "env_var_name": env_var, "restart": [app_id, req.target_id]}


@router.delete("/{app_id}/connections/{kind}/{link_id}")
async def remove_connection(app_id: int, kind: str, link_id: int, db: AsyncSession = Depends(get_db),
                            _user: dict = Depends(_auth.require_permission("apps.configure")),
                            actor: str = Depends(_auth.get_current_actor)):
    if kind == "database":
        link = await db.get(DatabaseLink, link_id)
        if link is None or link.app_id != app_id:
            raise HTTPException(404, "Connection not found")
        await link_ops.remove_link(db, link, actor)
        return {"ok": True, "restart": [app_id]}
    if kind != "app":
        raise HTTPException(404, "Connection not found")
    link = await db.get(AppLink, link_id)
    if link is None or link.app_id != app_id:
        raise HTTPException(404, "Connection not found")
    target_id, env_var = link.target_app_id, link.env_var_name
    await db.delete(link)
    await log_audit(db, "app.disconnect", actor=actor, app_id=app_id, detail={"target_app_id": target_id, "env_var": env_var})
    await db.commit()
    return {"ok": True, "restart": [app_id, target_id]}
