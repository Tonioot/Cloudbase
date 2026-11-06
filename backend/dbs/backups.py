import asyncio
import logging
import os
from datetime import timedelta, datetime
from typing import Optional
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from dbs import engines as db_engines, offsite as db_offsite, state as dbs_state, ops as dbs_ops, provision as dbs_provision
from dbs import backup_restore as dbs_backup_restore
from notify import emit as notify_emit
from core.audit import log_audit
from core.database import DATA_DIR, AsyncSessionLocal
from core.models import DatabaseBackup, Database, Node
from nodes.registry import get_node_by_token_or_401

log = logging.getLogger("cloudbase.backups")


BACKUP_DIR = os.path.join(DATA_DIR, "backups")


BACKUP_TIMEOUT_SECONDS = 6 * 3600


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
        "offsite_status": b.offsite_status,
        "offsite_error": b.offsite_error,
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


async def _remove_everywhere(backup: DatabaseBackup) -> None:
    """Delete the local file and the off-site copy."""
    _remove_file(backup)
    if backup.offsite_key:
        async with AsyncSessionLocal() as db:
            settings = await db_offsite.load(db)
        if db_offsite.is_active(settings):
            try:
                await asyncio.to_thread(db_offsite.delete, settings, backup.offsite_key)
            except Exception as e:
                log.warning("[offsite] could not delete %s: %s", backup.offsite_key, e)


async def _ensure_local(backup: DatabaseBackup) -> bool:
    """Make sure the backup file is on this disk, fetching it from off-site storage
    when the local copy is gone (e.g. after a disk replacement)."""
    if os.path.exists(_path(backup)):
        return True
    if not backup.offsite_key:
        return False
    async with AsyncSessionLocal() as db:
        settings = await db_offsite.load(db)
    if not db_offsite.is_active(settings):
        return False
    try:
        if backup.offsite_key.endswith(db_offsite.ENCRYPTED_SUFFIX):
            encrypted = _path(backup) + db_offsite.ENCRYPTED_SUFFIX
            await asyncio.to_thread(db_offsite.download, settings, backup.offsite_key, encrypted)
            try:
                await asyncio.to_thread(db_offsite.decrypt_file, encrypted, _path(backup), db_offsite.keyring(settings))
                os.chmod(_path(backup), 0o600)
            finally:
                os.remove(encrypted)
        else:
            await asyncio.to_thread(db_offsite.download, settings, backup.offsite_key, _path(backup))
        log.info("[offsite] restored local copy of backup %s from storage", backup.id)
        return True
    except Exception as e:
        log.warning("[offsite] could not fetch backup %s: %s", backup.id, e)
        return False


async def _offsite_upload(backup_id: int) -> None:
    """Copy one backup to S3-compatible storage, if configured."""
    async with AsyncSessionLocal() as db:
        settings = await db_offsite.load(db)
        backup = await db.get(DatabaseBackup, backup_id)
        if backup is None or backup.status != "success" or not db_offsite.is_active(settings):
            return
        if not os.path.exists(_path(backup)):
            return
        key = db_offsite.object_key(settings, backup.file_name)
        path = _path(backup)
    encrypted_path = None
    try:
        if settings.get("encrypt"):
            secret = db_offsite.current_key(settings)
            if not secret:
                raise RuntimeError("Encryption is on but no key is set")
            encrypted_path = path + db_offsite.ENCRYPTED_SUFFIX
            await asyncio.to_thread(db_offsite.encrypt_file, path, encrypted_path, secret)
            key += db_offsite.ENCRYPTED_SUFFIX
        await asyncio.to_thread(db_offsite.upload, settings, encrypted_path or path, key)
        status, error = "uploaded", None
        log.info("[offsite] backup %s uploaded to %s", backup_id, key)
    except Exception as e:
        status, error = "failed", str(e)[:1000]
        log.warning("[offsite] backup %s upload failed: %s", backup_id, e)
    finally:
        if encrypted_path and os.path.exists(encrypted_path):
            os.remove(encrypted_path)
    async with AsyncSessionLocal() as db:
        backup = await db.get(DatabaseBackup, backup_id)
        if backup:
            backup.offsite_status, backup.offsite_error, backup.offsite_at = status, error, datetime.utcnow()
            backup.offsite_key = key if status == "uploaded" else backup.offsite_key
            await db.commit()
    # One problem for the storage as a whole: it's retried hourly and usually a settings issue
    if status == "failed":
        await notify_emit.emit("backup.offsite_failed", "Off-site backup upload failed",
                          f"{error}\nCheck Settings → Backup storage. Cloudbase retries every hour.",
                          link="/settings?s=backups", key="backup.offsite_failed")
    else:
        await notify_emit.resolve("backup.offsite_failed", "Off-site backup uploads work again")


async def _offsite_catch_up(limit: int = 3) -> None:
    """Upload backups that have no off-site copy yet; retry failures hourly."""
    async with AsyncSessionLocal() as db:
        settings = await db_offsite.load(db)
        if not db_offsite.is_active(settings):
            return
        retry_before = datetime.utcnow() - timedelta(hours=1)
        pending = (await db.execute(
            select(DatabaseBackup.id).where(
                DatabaseBackup.status == "success",
                (DatabaseBackup.offsite_status.is_(None))
                | ((DatabaseBackup.offsite_status == "failed") & (DatabaseBackup.offsite_at < retry_before)),
            ).order_by(DatabaseBackup.created_at.desc()).limit(limit)
        )).scalars().all()
    for backup_id in pending:
        await _offsite_upload(backup_id)


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
        spec = dbs_provision._spec(database)
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
            info = await asyncio.to_thread(dbs_backup_restore.backup_to_file, spec, path)
        else:
            # The agent dumps on its node and streams the file to the upload endpoint below.
            await dbs_ops._run_on_node(node, None, "db_backup", {"spec": spec, "backup_id": backup_id},
                                   timeout=BACKUP_TIMEOUT_SECONDS)
            if not os.path.exists(path):
                raise RuntimeError("The node reported success, but the backup file never arrived")
            info = {"size_bytes": os.path.getsize(path), "sha256": await asyncio.to_thread(dbs_backup_restore._sha256, path)}
    except Exception as e:
        async with AsyncSessionLocal() as db:
            backup = await db.get(DatabaseBackup, backup_id)
            if backup:
                backup.status, backup.error, backup.finished_at = "failed", str(e)[:2000], datetime.utcnow()
                await db.commit()
        log.warning("[backup] database %s (%s): %s", database_id, trigger, e)
        await notify_emit.emit("backup.failed", f"Backup of database {spec['name']} failed",
                          str(e)[:1000], link=f"/database?id={database_id}", key=f"backup.failed:{database_id}")
        raise

    async with AsyncSessionLocal() as db:
        backup = await db.get(DatabaseBackup, backup_id)
        backup.status, backup.finished_at = "success", datetime.utcnow()
        backup.size_bytes, backup.sha256 = info["size_bytes"], info["sha256"]
        await db.commit()
    log.info("[backup] database %s: %s backup %s done (%s bytes)", database_id, trigger, backup_id, info["size_bytes"])
    await notify_emit.resolve(f"backup.failed:{database_id}", f"Backups of database {spec['name']} work again",
                         f"Backup {backup_id} succeeded.")
    asyncio.create_task(_offsite_upload(backup_id))
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
            await _remove_everywhere(backup)
            await db.delete(backup)
        await db.commit()


def _start_in_background(database_id: int, operation: str, coro_fn) -> None:
    """One backup or restore at a time per database; the monitor and lifecycle
    actions wait. Failures are recorded on the backup, never on the database."""
    dbs_state._busy.add(database_id)
    dbs_state._operation[database_id] = operation

    async def runner():
        try:
            await coro_fn()
        except Exception:
            pass
        finally:
            dbs_state._busy.discard(database_id)
            dbs_state._operation.pop(database_id, None)

    asyncio.create_task(runner())


def _require_backup_ready(database: Database, node: Optional[Node]) -> None:
    dbs_ops._require_idle(database)
    if node is None or not dbs_ops._node_reachable(node):
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
            spec = dbs_provision._spec(database)
            path, source = _path(backup), backup.source_db_name
        if not await _ensure_local(backup):
            raise RuntimeError("The backup file is gone and no off-site copy could be fetched")
        if node.is_local:
            await asyncio.to_thread(dbs_backup_restore.restore_from_file, spec, path, source)
        else:
            await dbs_ops._run_on_node(node, None, "db_restore",
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


# ── Node agent endpoints ──────────────────────────────────────────────────────

async def _node_for_database(database_id: int, token: Optional[str], db: AsyncSession) -> Database:
    if not token:
        raise HTTPException(401, "Missing node token")
    node = await get_node_by_token_or_401(token, db)
    database = await db.get(Database, database_id)
    if database is None or database.node_id != node.id:
        raise HTTPException(403, "This database does not run on your node")
    return database
