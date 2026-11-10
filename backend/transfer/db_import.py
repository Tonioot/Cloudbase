"""The databases part of an import (see databases.py for what the file holds).

Each database is created exactly like one made in the panel (same checks, same provisioning), with
new credentials. Linked apps are attached by name. When the file has the data, it is stored as a
normal backup of the new database and restored as soon as the database is running, in the
background: restoring a large dump can take a long time.
"""
import asyncio
import hashlib
import logging
import os
import tarfile
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.audit import log_audit
from core.database import AsyncSessionLocal
from core.env_crypto import decrypt_env
from core.models import Application, Database, DatabaseBackup, DatabaseLink
from dbs import engines as db_engines
from dbs import links as db_links
from dbs.backups import SCHEDULES, _path, _restore
from dbs.create import create_database_now
from dbs.provision import ENV_VAR_RE
from dbs.public import sync_public_access
from transfer import archive

log = logging.getLogger("cloudbase.transfer")

RESTORE_WAIT_SECONDS = 20 * 60        # how long to wait for a new database to come up before restoring


@dataclass
class ImportedDatabase:
    name: str
    engine: str
    version: str
    links: int
    restoring: bool


def _allowed(perms, name: str) -> bool:
    return perms is None or name in perms          # None: the root account


async def import_databases(
    db: AsyncSession,
    tar: tarfile.TarFile,
    entries: list[dict],
    app_ids: dict[str, int],
    target_node_id: Optional[int],
    perms,
    actor: str,
    warnings: list[str],
) -> tuple[list[ImportedDatabase], list[dict]]:
    imported, skipped = [], []
    linked_any = False
    for entry in entries:
        name = str(entry.get("name") or "").strip()
        try:
            if not _allowed(perms, "databases.create"):
                raise HTTPException(403, "you need the 'databases.create' permission to import databases")
            result = await _import_one(db, tar, entry, app_ids, target_node_id, perms, actor, warnings)
        except HTTPException as exc:
            await db.rollback()
            skipped.append({"name": name or "(no name)", "reason": str(exc.detail)})
            continue
        except archive.ArchiveError as exc:
            await db.rollback()
            skipped.append({"name": name or "(no name)", "reason": str(exc)})
            continue
        except Exception as exc:                       # one broken entry must not stop the others
            await db.rollback()
            log.exception("[transfer] import of database %r failed", name)
            skipped.append({"name": name or "(no name)", "reason": f"unexpected error: {exc}"})
            continue
        imported.append(result)
        linked_any = linked_any or result.links > 0
    if linked_any:
        await db_links.refresh(db)
        await sync_public_access()
    return imported, skipped


async def _import_one(db, tar, entry, app_ids, target_node_id, perms, actor, warnings) -> ImportedDatabase:
    if entry.get("data"):
        archive.open_data(tar, str(entry["data"].get("file") or ""))     # fail before anything is created
    database, node = await create_database_now(
        db, name_in=entry.get("name"), engine_in=entry.get("engine"), version_in=entry.get("version"),
        node_id=target_node_id, cpu_limit=entry.get("cpu_limit"), memory_limit_mb=entry.get("memory_limit_mb"), actor=actor)
    database_id, name, engine_key, version = database.id, database.name, database.engine, database.version   # plain values: a rollback expires the objects
    engine = db_engines.get_engine(engine_key)

    row = await db.get(Database, database_id)
    if entry.get("backup_schedule") in SCHEDULES:
        row.backup_schedule = entry["backup_schedule"]
        for column in ("backup_hour", "backup_retention"):
            if isinstance(entry.get(column), int):
                setattr(row, column, entry[column])
    await db.commit()
    if entry.get("public_was_enabled"):
        warnings.append(f"{name}: it was reachable from the internet; that is off in the import, turn it on again if you need it")

    links = await _link_apps(db, database_id, name, entry.get("links") or [], app_ids, perms, actor, warnings)

    restoring = False
    data = entry.get("data")
    if data:
        if not _allowed(perms, "databases.backup"):
            warnings.append(f"{name}: its data was not restored (you need the 'databases.backup' permission)")
        elif not engine.can_restore:
            warnings.append(f"{name}: {engine.label} cannot be restored from a dump yet, so its data was not restored")
        else:
            backup_id = await _store_dump(db, tar, database_id, name, engine, version, data, actor)
            asyncio.create_task(_restore_when_ready(database_id, backup_id, actor))
            restoring = True
    return ImportedDatabase(name=name, engine=engine_key, version=version, links=links, restoring=restoring)


async def _link_apps(db, database_id, name, links, app_ids, perms, actor, warnings) -> int:
    count = 0
    for link in links:
        app_name = str(link.get("app") or "")
        app_id = app_ids.get(app_name)
        if app_id is None:
            warnings.append(f"{name}: not linked to '{app_name}' (that app was not imported)")
            continue
        if not _allowed(perms, "databases.manage"):
            warnings.append(f"{name}: not linked to '{app_name}' (you need the 'databases.manage' permission)")
            continue
        env_var = str(link.get("env_var") or "").strip().upper()
        app = await db.get(Application, app_id)
        if not ENV_VAR_RE.match(env_var):
            warnings.append(f"{name}: link to '{app_name}' skipped (invalid variable name {env_var!r})")
            continue
        if env_var in decrypt_env(app.env_vars or ""):
            warnings.append(f"{name}: link to '{app_name}' skipped ({app_name} already sets {env_var} itself)")
            continue
        db.add(DatabaseLink(database_id=database_id, app_id=app_id, env_var_name=env_var))
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            warnings.append(f"{name}: '{app_name}' already gets {env_var} from a linked database")
            continue
        await log_audit(db, "database.link", actor=actor, app_id=app_id,
                        detail={"database_id": database_id, "name": name, "env_var": env_var})
        await db.commit()
        count += 1
    return count


async def _store_dump(db, tar, database_id, name, engine, version, data, actor) -> int:
    """Copy the dump out of the export into the backup folder, as a backup of the new database."""
    source = archive.open_data(tar, str(data.get("file") or ""))
    backup = DatabaseBackup(
        database_id=database_id, database_name=name, engine=engine.key, version=version,
        source_db_name=data.get("source_db_name"), status="running", trigger="import", actor=actor)
    db.add(backup)
    await db.flush()
    ext = engine.backup["ext"] if engine.backup else "dump"
    backup.file_name = f"{database_id}/{datetime.utcnow():%Y%m%d-%H%M%S}-import-{backup.id}.{ext}"
    await db.commit()
    backup_id, path = backup.id, _path(backup)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    def copy() -> tuple[int, str]:
        digest = hashlib.sha256()
        size = 0
        with open(path, "wb") as out:
            while chunk := source.read(1024 * 1024):
                out.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        os.chmod(path, 0o600)
        return size, digest.hexdigest()

    try:
        size, sha = await asyncio.to_thread(copy)
    except Exception:
        if os.path.exists(path):
            os.remove(path)
        raise
    backup = await db.get(DatabaseBackup, backup_id)
    backup.size_bytes, backup.sha256 = size, sha
    backup.status, backup.finished_at = "success", datetime.utcnow()
    await db.commit()
    return backup_id


async def _restore_when_ready(database_id: int, backup_id: int, actor: str) -> None:
    """Wait for the new database to run, then restore the imported dump into it."""
    deadline = time.monotonic() + RESTORE_WAIT_SECONDS
    while True:
        async with AsyncSessionLocal() as db:
            database = await db.get(Database, database_id)
        if database is None:
            return
        if database.status == "running":
            break
        if database.status == "error" or time.monotonic() > deadline:
            log.warning("[transfer] database %s never came up; its imported data was not restored (status %s)",
                        database_id, database.status)
            return
        await asyncio.sleep(3)
    await _restore(database_id, backup_id, False, actor)

