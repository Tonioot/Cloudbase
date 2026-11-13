"""Creating and removing a link between an app and a database. Shared by the database page and the app's
Connections tab, so both do exactly the same."""
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from apps import app_links
from core.audit import log_audit
from core.env_crypto import decrypt_env, encrypt_env
from core.models import Application, Database, DatabaseLink
from dbs import engines as db_engines, links as db_links
from dbs.provision import ENV_VAR_RE
from dbs.public import sync_public_access


async def add_link(db: AsyncSession, database: Database, app: Application, env_var_name: str | None,
                   actor: str, replace_env: bool = False) -> DatabaseLink:
    """Link an app to a database. `replace_env`: the app already sets that name itself; drop its own value."""
    app_name = app.name  # read before a possible rollback expires the instance
    env_var = (env_var_name or db_engines.get_engine(database.engine).env_var).strip().upper()
    if not ENV_VAR_RE.match(env_var):
        raise HTTPException(400, "Env var name may only contain A–Z, 0–9 and _, and may not start with a digit")
    own = decrypt_env(app.env_vars or "")
    if env_var in own and not replace_env:
        raise HTTPException(409, f"{app_name} already sets {env_var} itself. Replace it with the link, or pick another name.")
    if env_var in await app_links.reserved_env_names(db, app.id):
        raise HTTPException(409, f"{app_name} already gets {env_var} from another connection")
    if env_var in own:
        del own[env_var]
        app.env_vars = encrypt_env(own)

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
    await sync_public_access()
    return link


async def remove_link(db: AsyncSession, link: DatabaseLink, actor: str) -> None:
    database_id, app_id, env_var = link.database_id, link.app_id, link.env_var_name
    await db.delete(link)
    await log_audit(db, "database.unlink", actor=actor, app_id=app_id, detail={
        "database_id": database_id, "env_var": env_var,
    })
    await db.commit()
    await db_links.refresh(db)
    await sync_public_access()
