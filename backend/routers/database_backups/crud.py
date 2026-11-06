import asyncio
import os
from datetime import datetime
from fastapi import HTTPException, Depends, APIRouter, File, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from core import auth as _auth
from dbs import engines as db_engines, offsite as db_offsite, state as dbs_state, ops as dbs_ops
from dbs import backup_restore as dbs_backup_restore
from core.audit import log_audit
from core.database import get_db
from core.models import DatabaseBackup, Node
from dbs.backups import SCHEDULES, _as_dict, _backup_or_404, _download_name, _ensure_local, _offsite_upload, _path, _remove_everywhere, _remove_file, _require_backup_ready, _restore, _restores, _start_in_background, run_backup
from routers.database_backups.schemas import BackupSettingsRequest, RestoreRequest

router = APIRouter(prefix="/api/databases", tags=["database-backups"])


@router.get("/{database_id}/backups")
async def list_backups(
    database_id: int,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("databases.view")),
):
    database = await dbs_ops._get_or_404(database_id, db)
    own = (await db.execute(
        select(DatabaseBackup).where(DatabaseBackup.database_id == database_id)
        .order_by(DatabaseBackup.created_at.desc())
    )).scalars().all()
    # Backups of other or deleted databases with the same engine can be restored here too.
    others = (await db.execute(
        select(DatabaseBackup).where(
            DatabaseBackup.engine == database.engine, DatabaseBackup.status == "success",
            (DatabaseBackup.database_id != database_id) | (DatabaseBackup.database_id.is_(None)),
        ).order_by(DatabaseBackup.created_at.desc()).limit(50)
    )).scalars().all()
    engine = db_engines.get_engine(database.engine)
    storage = await db_offsite.load(db)
    return {
        "offsite": {"enabled": db_offsite.is_active(storage), "bucket": storage.get("bucket") or None},
        "backups": [_as_dict(b) for b in own],
        "others": [_as_dict(b) for b in others],
        "schedule": database.backup_schedule,
        "hour": database.backup_hour,
        "retention": database.backup_retention,
        "can_restore": engine.can_restore,
        "restore": _restores.get(database_id),
        "operation": dbs_state._operation.get(database_id),
    }


@router.put("/{database_id}/backup-settings")
async def update_backup_settings(
    database_id: int,
    req: BackupSettingsRequest,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("databases.backup")),
    actor: str = Depends(_auth.get_current_actor),
):
    if req.schedule not in SCHEDULES:
        raise HTTPException(400, f"Schedule must be one of: {', '.join(SCHEDULES)}")
    database = await dbs_ops._get_or_404(database_id, db)
    database.backup_schedule, database.backup_hour, database.backup_retention = req.schedule, req.hour, req.retention
    await log_audit(db, "database.backup_settings", actor=actor, detail={
        "database_id": database_id, "schedule": req.schedule, "hour": req.hour, "retention": req.retention,
    })
    await db.commit()
    return {"ok": True}


@router.post("/{database_id}/backups")
async def create_backup(
    database_id: int,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("databases.backup")),
    actor: str = Depends(_auth.get_current_actor),
):
    database = await dbs_ops._get_or_404(database_id, db)
    _require_backup_ready(database, await db.get(Node, database.node_id))
    await log_audit(db, "database.backup", actor=actor, detail={"database_id": database_id, "name": database.name})
    await db.commit()
    _start_in_background(database_id, "Backing up…", lambda: run_backup(database_id, "manual", actor))
    return {"ok": True}


@router.post("/{database_id}/backups/upload")
async def upload_backup(
    database_id: int,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("databases.backup")),
    actor: str = Depends(_auth.get_current_actor),
):
    """Store a dump made elsewhere (e.g. mongodump from Atlas) so it can be restored."""
    database = await dbs_ops._get_or_404(database_id, db)
    original = os.path.basename(file.filename or "upload")
    backup = DatabaseBackup(
        database_id=database.id, database_name=database.name, engine=database.engine,
        version=None, source_db_name=None, status="running", trigger="upload", actor=actor,
    )
    db.add(backup)
    await db.flush()
    suffix = original.split(".", 1)[1] if "." in original else "dump"
    backup.file_name = f"{database.id}/{datetime.utcnow():%Y%m%d-%H%M%S}-upload-{backup.id}.{suffix[:20]}"
    await db.commit()
    path = _path(backup)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                out.write(chunk)
        os.chmod(path, 0o600)
        backup.size_bytes = os.path.getsize(path)
        backup.sha256 = await asyncio.to_thread(dbs_backup_restore._sha256, path)
        backup.status, backup.finished_at = "success", datetime.utcnow()
    except Exception as e:
        backup.status, backup.error = "failed", str(e)[:2000]
        _remove_file(backup)
    await log_audit(db, "database.backup_upload", actor=actor, detail={
        "database_id": database_id, "backup_id": backup.id, "file": original, "size": backup.size_bytes,
    })
    await db.commit()
    if backup.status == "success":
        asyncio.create_task(_offsite_upload(backup.id))
    return _as_dict(backup)


@router.get("/{database_id}/backups/{backup_id}/download")
async def download_backup(
    database_id: int,
    backup_id: int,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("databases.backup")),
    actor: str = Depends(_auth.get_current_actor),
):
    backup = await _backup_or_404(database_id, backup_id, db)
    if backup.status != "success" or not await _ensure_local(backup):
        raise HTTPException(404, "Backup file not available")
    await log_audit(db, "database.backup_download", actor=actor, detail={"database_id": database_id, "backup_id": backup_id})
    await db.commit()
    return FileResponse(_path(backup), filename=_download_name(backup), media_type="application/octet-stream")


@router.delete("/{database_id}/backups/{backup_id}")
async def delete_backup(
    database_id: int,
    backup_id: int,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("databases.backup")),
    actor: str = Depends(_auth.get_current_actor),
):
    backup = await _backup_or_404(database_id, backup_id, db)
    if backup.status == "running":
        raise HTTPException(409, "This backup is still running")
    await _remove_everywhere(backup)
    await db.delete(backup)
    await log_audit(db, "database.backup_delete", actor=actor, detail={"database_id": database_id, "backup_id": backup_id})
    await db.commit()
    return {"ok": True}


@router.post("/{database_id}/restore")
async def restore_backup(
    database_id: int,
    req: RestoreRequest,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("databases.backup")),
    actor: str = Depends(_auth.get_current_actor),
):
    database = await dbs_ops._get_or_404(database_id, db)
    _require_backup_ready(database, await db.get(Node, database.node_id))
    if not db_engines.get_engine(database.engine).can_restore:
        raise HTTPException(409, f"Restoring {database.engine} backups is not supported yet")
    backup = await db.get(DatabaseBackup, req.backup_id)
    if backup is None or backup.status != "success" or not (os.path.exists(_path(backup)) or backup.offsite_key):
        raise HTTPException(404, "Backup not found")
    if backup.engine != database.engine:
        raise HTTPException(400, f"This is a {backup.engine} backup; {database.name} runs {database.engine}")
    _start_in_background(database_id, "Restoring backup…",
                         lambda: _restore(database_id, backup.id, req.safety_backup, actor))
    return {"ok": True}
