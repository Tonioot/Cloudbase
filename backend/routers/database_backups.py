"""Database backups: scheduled and manual dumps, uploads, downloads and restores.

Every backup ends up on the primary in ~/.cloudbase/backups/<database id>/, also
for databases on other nodes (their agent streams the dump here). Backups outlive
their database, so a deleted database can be restored into a new one.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import auth as _auth
import db_engines
import db_manager
from audit import log_audit
from database import AsyncSessionLocal, DATA_DIR, get_db
from models import Database, DatabaseBackup, Node
from routers import databases as dbs
from routers.nodes import get_node_by_token_or_401

router = APIRouter(prefix="/api/databases", tags=["database-backups"])
agent_router = APIRouter(prefix="/api/nodes/agent/databases", tags=["database-backups"])
log = logging.getLogger("cloudbase.backups")

BACKUP_DIR = os.path.join(DATA_DIR, "backups")
BACKUP_TIMEOUT_SECONDS = 6 * 3600
SCHEDULER_INTERVAL_SECONDS = 60
FAILED_RETRY_SECONDS = 3600
SCHEDULES = ("off", "hourly", "daily")

# Restore progress per database: {state: running|success|failed, message, backup_id, at}
_restores: dict[int, dict] = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _path(backup: DatabaseBackup) -> str:
    return os.path.join(BACKUP_DIR, backup.file_name)


def _as_dict(b: DatabaseBackup) -> dict:
    return {
        "id": b.id,
        "database_id": b.database_id,
        "database_name": b.database_name,
        "engine": b.engine,
        "version": b.version,
        "size_bytes": b.size_bytes,
        "status": b.status,
        "trigger": b.trigger,
        "error": b.error,
        "actor": b.actor,
        "created_at": b.created_at.isoformat() if b.created_at else None,
        "finished_at": b.finished_at.isoformat() if b.finished_at else None,
    }


def _download_name(b: DatabaseBackup) -> str:
    ext = b.file_name.split(".", 1)[1] if b.file_name and "." in b.file_name else "dump"
    return f"{b.database_name}-{b.created_at:%Y%m%d-%H%M%S}.{ext}"


async def _backup_or_404(database_id: int, backup_id: int, db: AsyncSession) -> DatabaseBackup:
    backup = await db.get(DatabaseBackup, backup_id)
    if backup is None or backup.database_id != database_id:
        raise HTTPException(404, "Backup not found")
    return backup


def _remove_file(backup: DatabaseBackup) -> None:
    if backup.file_name:
        try:
            os.remove(_path(backup))
        except FileNotFoundError:
            pass


# ── Making a backup ───────────────────────────────────────────────────────────

async def run_backup(database_id: int, trigger: str, actor: Optional[str] = None) -> int:
    """Dump a database to the primary and wait for it. Returns the backup id;
    raises when the backup failed (the row then says why)."""
    async with AsyncSessionLocal() as db:
        database = await db.get(Database, database_id)
        if database is None:
            raise RuntimeError("Database not found")
        node = await db.get(Node, database.node_id)
        engine = db_engines.get_engine(database.engine)
        if not engine.backup:
            raise RuntimeError(f"{engine.label} backups are not supported")
        spec = dbs._spec(database)
        backup = DatabaseBackup(
            database_id=database.id, database_name=database.name, engine=database.engine,
            version=database.version, source_db_name=spec["credentials"].get("db_name"),
            status="running", trigger=trigger, actor=actor,
        )
        db.add(backup)
        await db.flush()
        backup.file_name = f"{database.id}/{datetime.utcnow():%Y%m%d-%H%M%S}-{trigger}-{backup.id}.{engine.backup['ext']}"
        await db.commit()
        backup_id, path = backup.id, _path(backup)

    try:
        if node is None:
            raise RuntimeError("The node of this database no longer exists")
        if node.is_local:
            info = await asyncio.to_thread(db_manager.backup_to_file, spec, path)
        else:
            # The agent dumps on its node and streams the file to the upload endpoint below.
            await dbs._run_on_node(node, None, "db_backup", {"spec": spec, "backup_id": backup_id},
                                   timeout=BACKUP_TIMEOUT_SECONDS)
            if not os.path.exists(path):
                raise RuntimeError("The node reported success, but the backup file never arrived")
            info = {"size_bytes": os.path.getsize(path), "sha256": await asyncio.to_thread(db_manager._sha256, path)}
    except Exception as e:
        async with AsyncSessionLocal() as db:
            backup = await db.get(DatabaseBackup, backup_id)
            if backup:
                backup.status, backup.error, backup.finished_at = "failed", str(e)[:2000], datetime.utcnow()
                await db.commit()
        log.warning("[backup] database %s (%s): %s", database_id, trigger, e)
        raise

    async with AsyncSessionLocal() as db:
        backup = await db.get(DatabaseBackup, backup_id)
        backup.status, backup.finished_at = "success", datetime.utcnow()
        backup.size_bytes, backup.sha256 = info["size_bytes"], info["sha256"]
        await db.commit()
    log.info("[backup] database %s: %s backup %s done (%s bytes)", database_id, trigger, backup_id, info["size_bytes"])
    return backup_id


async def _apply_retention(database_id: int) -> None:
    """Keep the newest N successful scheduled backups; drop failed ones after a week."""
    async with AsyncSessionLocal() as db:
        database = await db.get(Database, database_id)
        if database is None:
            return
        scheduled = (await db.execute(
            select(DatabaseBackup)
            .where(DatabaseBackup.database_id == database_id, DatabaseBackup.trigger == "scheduled",
                   DatabaseBackup.status == "success")
            .order_by(DatabaseBackup.created_at.desc())
        )).scalars().all()
        old_failed = (await db.execute(
            select(DatabaseBackup).where(
                DatabaseBackup.database_id == database_id, DatabaseBackup.status == "failed",
                DatabaseBackup.created_at < datetime.utcnow() - timedelta(days=7),
            )
        )).scalars().all()
        for backup in list(scheduled[max(database.backup_retention, 1):]) + list(old_failed):
            _remove_file(backup)
            await db.delete(backup)
        await db.commit()


def _start_in_background(database_id: int, operation: str, coro_fn) -> None:
    """One backup or restore at a time per database; the monitor and lifecycle
    actions wait. Failures are recorded on the backup, never on the database."""
    dbs._busy.add(database_id)
    dbs._operation[database_id] = operation

    async def runner():
        try:
            await coro_fn()
        except Exception:
            pass
        finally:
            dbs._busy.discard(database_id)
            dbs._operation.pop(database_id, None)

    asyncio.create_task(runner())


def _require_backup_ready(database: Database, node: Optional[Node]) -> None:
    dbs._require_idle(database)
    if node is None or not dbs._node_reachable(node):
        raise HTTPException(409, "The node of this database is offline")
    if database.status not in ("running", "unhealthy"):
        raise HTTPException(409, "The database must be running")


# ── Restoring ─────────────────────────────────────────────────────────────────

async def _restore(database_id: int, backup_id: int, safety_backup: bool, actor: str) -> None:
    _restores[database_id] = {"state": "running", "backup_id": backup_id, "message": None,
                              "at": datetime.utcnow().isoformat()}
    try:
        if safety_backup:
            _restores[database_id]["message"] = "Making a safety backup first…"
            await run_backup(database_id, "pre_restore", actor)
        _restores[database_id]["message"] = "Restoring…"
        async with AsyncSessionLocal() as db:
            database = await db.get(Database, database_id)
            backup = await db.get(DatabaseBackup, backup_id)
            node = await db.get(Node, database.node_id)
            spec = dbs._spec(database)
            path, source = _path(backup), backup.source_db_name
        if node.is_local:
            await asyncio.to_thread(db_manager.restore_from_file, spec, path, source)
        else:
            await dbs._run_on_node(node, None, "db_restore",
                                   {"spec": spec, "backup_id": backup_id, "source_db_name": source},
                                   timeout=BACKUP_TIMEOUT_SECONDS)
        _restores[database_id].update(state="success", message="Restore finished", at=datetime.utcnow().isoformat())
        ok, detail = True, None
    except Exception as e:
        _restores[database_id].update(state="failed", message=str(e)[:2000], at=datetime.utcnow().isoformat())
        ok, detail = False, str(e)[:500]
    async with AsyncSessionLocal() as db:
        await log_audit(db, "database.restore", actor=actor, detail={
            "database_id": database_id, "backup_id": backup_id, "ok": ok, "error": detail,
        })
        await db.commit()


# ── Endpoints ─────────────────────────────────────────────────────────────────

class BackupSettingsRequest(BaseModel):
    schedule: str
    hour: int = Field(default=3, ge=0, le=23)
    retention: int = Field(default=7, ge=1, le=365)


class RestoreRequest(BaseModel):
    backup_id: int
    safety_backup: bool = True


@router.get("/{database_id}/backups")
async def list_backups(
    database_id: int,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("databases.view")),
):
    database = await dbs._get_or_404(database_id, db)
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
    return {
        "backups": [_as_dict(b) for b in own],
        "others": [_as_dict(b) for b in others],
        "schedule": database.backup_schedule,
        "hour": database.backup_hour,
        "retention": database.backup_retention,
        "can_restore": engine.restore is not None,
        "restore": _restores.get(database_id),
        "operation": dbs._operation.get(database_id),
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
    database = await dbs._get_or_404(database_id, db)
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
    database = await dbs._get_or_404(database_id, db)
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
    database = await dbs._get_or_404(database_id, db)
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
        backup.sha256 = await asyncio.to_thread(db_manager._sha256, path)
        backup.status, backup.finished_at = "success", datetime.utcnow()
    except Exception as e:
        backup.status, backup.error = "failed", str(e)[:2000]
        _remove_file(backup)
    await log_audit(db, "database.backup_upload", actor=actor, detail={
        "database_id": database_id, "backup_id": backup.id, "file": original, "size": backup.size_bytes,
    })
    await db.commit()
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
    if backup.status != "success" or not os.path.exists(_path(backup)):
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
    _remove_file(backup)
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
    database = await dbs._get_or_404(database_id, db)
    _require_backup_ready(database, await db.get(Node, database.node_id))
    if db_engines.get_engine(database.engine).restore is None:
        raise HTTPException(409, f"Restoring {database.engine} backups is not supported yet")
    backup = await db.get(DatabaseBackup, req.backup_id)
    if backup is None or backup.status != "success" or not os.path.exists(_path(backup)):
        raise HTTPException(404, "Backup not found")
    if backup.engine != database.engine:
        raise HTTPException(400, f"This is a {backup.engine} backup; {database.name} runs {database.engine}")
    _start_in_background(database_id, "Restoring backup…",
                         lambda: _restore(database_id, backup.id, req.safety_backup, actor))
    return {"ok": True}


# ── Node agent endpoints ──────────────────────────────────────────────────────

async def _node_for_database(database_id: int, token: Optional[str], db: AsyncSession) -> Database:
    if not token:
        raise HTTPException(401, "Missing node token")
    node = await get_node_by_token_or_401(token, db)
    database = await db.get(Database, database_id)
    if database is None or database.node_id != node.id:
        raise HTTPException(403, "This database does not run on your node")
    return database


@agent_router.post("/{database_id}/backups/{backup_id}/upload")
async def agent_upload_backup(
    database_id: int,
    backup_id: int,
    request: Request,
    x_node_token: Optional[str] = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    await _node_for_database(database_id, x_node_token, db)
    backup = await db.get(DatabaseBackup, backup_id)
    if backup is None or backup.database_id != database_id or backup.status != "running":
        raise HTTPException(409, "No running backup with this id")
    path = _path(backup)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    part = path + ".part"
    with open(part, "wb") as out:
        async for chunk in request.stream():
            out.write(chunk)
    os.replace(part, path)
    os.chmod(path, 0o600)
    return {"ok": True, "size_bytes": os.path.getsize(path)}


@agent_router.get("/{database_id}/backups/{backup_id}/download")
async def agent_download_backup(
    database_id: int,
    backup_id: int,
    x_node_token: Optional[str] = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    await _node_for_database(database_id, x_node_token, db)
    restore = _restores.get(database_id) or {}
    if restore.get("state") != "running" or restore.get("backup_id") != backup_id:
        raise HTTPException(403, "No restore of this backup is in progress for this database")
    backup = await db.get(DatabaseBackup, backup_id)
    if backup is None or not os.path.exists(_path(backup)):
        raise HTTPException(404, "Backup file not available")
    return FileResponse(_path(backup), media_type="application/octet-stream")


# ── Scheduler ─────────────────────────────────────────────────────────────────

def _is_due(database: Database, last: Optional[DatabaseBackup], now_utc: datetime) -> bool:
    if database.backup_schedule == "off":
        return False
    if last is not None and last.status == "running":
        return False
    if last is not None and last.status == "failed":
        return now_utc - last.created_at >= timedelta(seconds=FAILED_RETRY_SECONDS)
    if database.backup_schedule == "hourly":
        return last is None or now_utc - last.created_at >= timedelta(minutes=55)
    # Daily at backup_hour, server time; catches up after downtime.
    local_now = datetime.now().astimezone()
    slot = local_now.replace(hour=database.backup_hour, minute=0, second=0, microsecond=0)
    if local_now < slot:
        slot -= timedelta(days=1)
    slot_utc = slot.astimezone(timezone.utc).replace(tzinfo=None)
    return last is None or last.created_at < slot_utc


async def _scheduler_tick() -> None:
    now_utc = datetime.utcnow()
    async with AsyncSessionLocal() as db:
        candidates = (await db.execute(
            select(Database).where(
                Database.backup_schedule != "off", Database.desired_state == "running",
                Database.status.in_(["running", "unhealthy"]), Database.initialized_at.is_not(None),
            )
        )).scalars().all()
        due = []
        for database in candidates:
            if database.id in dbs._busy or not db_engines.get_engine(database.engine).backup:
                continue
            last = (await db.execute(
                select(DatabaseBackup)
                .where(DatabaseBackup.database_id == database.id, DatabaseBackup.trigger == "scheduled")
                .order_by(DatabaseBackup.created_at.desc()).limit(1)
            )).scalar_one_or_none()
            if _is_due(database, last, now_utc):
                due.append(database.id)
    # One at a time: backups are heavy on disk and network.
    for database_id in due:
        if database_id in dbs._busy:
            continue
        dbs._busy.add(database_id)
        dbs._operation[database_id] = "Backing up…"
        try:
            await run_backup(database_id, "scheduled", "scheduler")
            await _apply_retention(database_id)
        except Exception:
            pass
        finally:
            dbs._busy.discard(database_id)
            dbs._operation.pop(database_id, None)


async def backup_scheduler() -> None:
    """Run due scheduled backups (primary only)."""
    await asyncio.sleep(90)
    while True:
        try:
            await _scheduler_tick()
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.warning("[backup-scheduler] error: %s", e)
        await asyncio.sleep(SCHEDULER_INTERVAL_SECONDS)
