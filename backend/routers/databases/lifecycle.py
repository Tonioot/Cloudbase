import json
import secrets
from datetime import datetime
from fastapi import HTTPException, APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from core import auth as _auth
from dbs import links as db_links
from dbs import backup_restore as dbs_backup_restore
from dbs import lifecycle as dbs_lifecycle
from dbs import updates as dbs_updates
from core.audit import log_audit
from core.database import AsyncSessionLocal, get_db
from core.env_crypto import encrypt_text
from core.models import Database
from dbs.ops import _begin, _get_or_404, _links_for, _node_for, _node_reachable, _run_on_node, _set_status, _spawn, _start_operation
from dbs.provision import _spec
from dbs.state import PROVISION_TIMEOUT_SECONDS, _operation, _update_info, claim, release
from dbs.workers import _check_updates_on
from dbs.backups import run_backup

router = APIRouter(prefix="/api/databases", tags=["databases"])


@router.post("/{database_id}/start")
async def start_database(
    database_id: int,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("databases.manage")),
    actor: str = Depends(_auth.get_current_actor),
):
    database, node = await _begin(database_id, db)
    database.desired_state = "running"
    database.status = "starting"
    database.last_error = None
    await log_audit(db, "database.start", actor=actor, detail={"database_id": database.id, "name": database.name})
    await db.commit()
    _spawn(database.id, lambda: _start_operation(database.id, "ensure"))
    return {"ok": True}


@router.post("/{database_id}/restart")
async def restart_database(
    database_id: int,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("databases.manage")),
    actor: str = Depends(_auth.get_current_actor),
):
    database, node = await _begin(database_id, db)
    database.desired_state = "running"
    database.status = "starting"
    database.last_error = None
    await log_audit(db, "database.restart", actor=actor, detail={"database_id": database.id, "name": database.name})
    await db.commit()
    _spawn(database.id, lambda: _start_operation(database.id, "restart"))
    return {"ok": True}


@router.post("/{database_id}/stop")
async def stop_database(
    database_id: int,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("databases.manage")),
    actor: str = Depends(_auth.get_current_actor),
):
    database, node = await _begin(database_id, db)
    database.desired_state = "stopped"
    database.status = "stopping"
    database.last_error = None
    await log_audit(db, "database.stop", actor=actor, detail={"database_id": database.id, "name": database.name})
    await db.commit()

    async def op():
        await _run_on_node(node, lambda: dbs_lifecycle.stop(database_id), "db_stop", {"database_id": database_id})
        await _set_status(database_id, "stopped")

    _spawn(database.id, op)
    return {"ok": True}


@router.post("/{database_id}/rotate-password")
async def rotate_password(
    database_id: int,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("databases.credentials")),
    actor: str = Depends(_auth.get_current_actor),
):
    """Give the app user a new password. The old one stops working at once; linked
    apps pick up the new URL when they restart."""
    database, node = await _begin(database_id, db, "Changing the password…")
    try:
        if database.status not in ("running", "unhealthy"):
            raise HTTPException(409, "The database must be running to change its password")
        spec = _spec(database)
        new_password = secrets.token_urlsafe(24)
        try:
            await _run_on_node(
                node,
                lambda: dbs_backup_restore.rotate_password(spec, new_password),
                "db_rotate_password",
                {"spec": spec, "new_password": new_password},
            )
        except Exception as e:
            raise HTTPException(502, f"The password was not changed: {e}")
        creds = {**spec["credentials"], "password": new_password}
        database.credentials = encrypt_text(json.dumps(creds))
        await log_audit(db, "database.rotate_password", actor=actor, detail={"database_id": database.id, "name": database.name})
        await db.commit()
    finally:
        release(database_id)

    await db_links.refresh(db)
    if database.engine in dbs_backup_restore.ROTATE_RECREATES:
        # The server command / environment carries the password: new container, same data.
        claim(database.id)
        database.status = "starting"
        await db.commit()
        _spawn(database.id, lambda: _start_operation(database.id, "recreate"))
    links = await _links_for(database_id, db)
    return {"ok": True, "apps": [{"id": l["app_id"], "name": l["app_name"]} for l in links]}


@router.post("/{database_id}/check-update")
async def check_update(
    database_id: int,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("databases.view")),
):
    database = await _get_or_404(database_id, db)
    node = await _node_for(database, db)
    if not _node_reachable(node):
        raise HTTPException(409, f"Node '{node.name}' is offline")
    await _check_updates_on(node, [database])
    return _update_info.get(database_id) or {}


@router.post("/{database_id}/update")
async def update_database_engine(
    database_id: int,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("databases.manage")),
    actor: str = Depends(_auth.get_current_actor),
):
    """Install the newest patch release of the database's version (e.g. 18.6 → 18.7).
    A backup is made first; a failed start rolls back to the previous image."""
    database, node = await _begin(database_id, db)
    if database.status not in ("running", "unhealthy"):
        release(database_id)
        raise HTTPException(409, "The database must be running to update it")
    await log_audit(db, "database.update_engine", actor=actor, detail={
        "database_id": database.id, "name": database.name, "from": (_update_info.get(database_id) or {}).get("version"),
    })
    await db.commit()

    async def op():
        _operation[database_id] = "Backing up before the update…"
        await run_backup(database_id, "pre_update", actor)
        _operation[database_id] = "Updating…"
        async with AsyncSessionLocal() as s:
            spec = _spec(await s.get(Database, database_id))
        try:
            result = await _run_on_node(node, lambda: dbs_updates.update(spec), "db_update", {"spec": spec},
                                        timeout=PROVISION_TIMEOUT_SECONDS + 900)
        finally:
            _operation.pop(database_id, None)
        _update_info[database_id] = {
            "version": result.get("version"), "update_available": False,
            "checked_at": datetime.utcnow().isoformat(), "updated_from": result.get("from"),
        }
        await _set_status(database_id, "running")

    _spawn(database.id, op)
    return {"ok": True}
