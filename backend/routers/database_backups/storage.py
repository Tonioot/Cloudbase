import asyncio
from fastapi import HTTPException, Depends, APIRouter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from core import auth as _auth
from dbs import offsite as db_offsite
from core.audit import log_audit
from core.database import get_db
from core.models import DatabaseBackup
from dbs.backups import _offsite_catch_up
from routers.database_backups.schemas import KeyRequest, StorageSettingsRequest

storage_router = APIRouter(prefix="/api/system/backup-storage", tags=["database-backups"])


@storage_router.get("")
async def get_storage(
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("system.manage")),
):
    settings = await db_offsite.load(db)
    totals = (await db.execute(
        select(DatabaseBackup.offsite_status).where(DatabaseBackup.status == "success")
    )).scalars().all()
    return {
        **db_offsite.public_view(settings),
        "counts": {
            "uploaded": sum(1 for s in totals if s == "uploaded"),
            "failed": sum(1 for s in totals if s == "failed"),
            "pending": sum(1 for s in totals if s is None),
        },
    }


@storage_router.put("")
async def put_storage(
    req: StorageSettingsRequest,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("system.manage")),
    actor: str = Depends(_auth.get_current_actor),
):
    settings = db_offsite.merge_update(await db_offsite.load(db), req.model_dump())
    if settings.get("enabled") and not db_offsite.is_active(settings):
        raise HTTPException(400, "Fill in the bucket, access key and secret key to turn off-site backups on")
    if settings.get("encrypt") and not settings.get("current_key_id"):
        settings = db_offsite.add_key(settings, db_offsite.new_key(), make_current=True)
    await db_offsite.save(db, settings)
    await log_audit(db, "system.backup_storage", actor=actor, detail={
        "enabled": bool(settings.get("enabled")), "endpoint": settings.get("endpoint"), "bucket": settings.get("bucket"),
    })
    await db.commit()
    if db_offsite.is_active(settings):
        asyncio.create_task(_offsite_catch_up(limit=20))
    return db_offsite.public_view(settings)


@storage_router.post("/key/reveal")
async def reveal_key(
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("system.manage")),
    actor: str = Depends(_auth.get_current_actor),
):
    """The current encryption key, to store somewhere safe off this server."""
    settings = await db_offsite.load(db)
    key = db_offsite.current_key(settings)
    if not key:
        raise HTTPException(404, "No encryption key yet; turn encryption on first")
    await log_audit(db, "system.backup_key_reveal", actor=actor, detail={"key_id": settings.get("current_key_id")})
    await db.commit()
    return {"key": key, "key_id": settings.get("current_key_id")}


@storage_router.post("/key")
async def add_key(
    req: KeyRequest,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("system.manage")),
    actor: str = Depends(_auth.get_current_actor),
):
    """Add a key, e.g. from an old server, so its encrypted backups can be read."""
    try:
        kid = db_offsite.key_id(req.key.strip())
    except ValueError as e:
        raise HTTPException(400, str(e))
    settings = db_offsite.add_key(await db_offsite.load(db), req.key.strip(), req.make_current)
    await db_offsite.save(db, settings)
    await log_audit(db, "system.backup_key_add", actor=actor, detail={"key_id": kid, "current": req.make_current})
    await db.commit()
    return db_offsite.public_view(settings)


@storage_router.post("/test")
async def test_storage(
    req: StorageSettingsRequest,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("system.manage")),
):
    """Try the settings in the form (not yet saved) with a small test object."""
    settings = db_offsite.merge_update(await db_offsite.load(db), req.model_dump())
    if not (settings.get("bucket") and settings.get("access_key") and settings.get("secret_key")):
        raise HTTPException(400, "Fill in the bucket, access key and secret key first")
    error = await asyncio.to_thread(db_offsite.test, settings)
    return {"ok": error is None, "error": error}
