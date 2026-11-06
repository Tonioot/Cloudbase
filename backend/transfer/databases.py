"""The databases part of an export.

Per database the file holds its settings, the apps it is linked to (by app name) and, when asked,
a dump of its data. What it never holds: the database's own credentials (an import creates new
ones; linked apps pick them up through the link) and whether it was reachable from the internet
(the import leaves that off and says so).
"""
import logging
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import AsyncSessionLocal
from core.models import Application, Database, DatabaseBackup, DatabaseLink
from dbs import engines as db_engines
from dbs.backups import _path, run_backup
from transfer import archive

log = logging.getLogger("cloudbase.transfer")


def _entry(database: Database, links: list[tuple[str, str]]) -> dict:
    return {
        "name": database.name,
        "engine": database.engine,
        "version": database.version,
        "cpu_limit": database.cpu_limit,
        "memory_limit_mb": database.memory_limit_mb,
        "backup_schedule": database.backup_schedule,
        "backup_hour": database.backup_hour,
        "backup_retention": database.backup_retention,
        "public_was_enabled": bool(database.public_enabled),
        "links": [{"app": app_name, "env_var": env_var} for app_name, env_var in links],
        "data": None,
    }


async def database_entries(db: AsyncSession, database_ids: Optional[list[int]]) -> tuple[list[dict], dict[str, int]]:
    """(entries, name -> id). None or an empty list: no databases."""
    if not database_ids:
        return [], {}
    rows = (await db.execute(select(Database).where(Database.id.in_(database_ids)).order_by(Database.name))).scalars().all()
    entries, ids = [], {}
    for database in rows:
        links = (await db.execute(
            select(Application.name, DatabaseLink.env_var_name)
            .join(DatabaseLink, DatabaseLink.app_id == Application.id)
            .where(DatabaseLink.database_id == database.id)
            .order_by(Application.name)
        )).all()
        entries.append(_entry(database, [(n, e) for n, e in links]))
        ids[database.name] = database.id
    return entries, ids


async def dump_into(entries: list[dict], ids: dict[str, int], actor: str, progress) -> dict[str, str]:
    """Take a fresh dump of every database and record it in its entry. Returns member name -> file.

    This is the normal backup mechanism, so each dump also shows up in the database's backup list
    (trigger "export") and follows the usual off-site copy and retention rules.
    """
    files: dict[str, str] = {}
    for index, entry in enumerate(entries):
        name = entry["name"]
        engine = db_engines.get_engine(entry["engine"])
        if not engine.backup:
            raise HTTPException(409, f"{engine.label} databases cannot be dumped, so '{name}' cannot be exported with its data")
        async with AsyncSessionLocal() as db:
            database = await db.get(Database, ids[name])
            if database is None or database.status not in ("running", "unhealthy"):
                raise HTTPException(409, f"Database '{name}' is not running. Start it, or export without its data")
        progress(f"Dumping database {name} ({index + 1} of {len(entries)})…")
        backup_id = await run_backup(ids[name], "export", actor)
        async with AsyncSessionLocal() as db:
            backup = await db.get(DatabaseBackup, backup_id)
            member = f"{archive.DATA_PREFIX}{index}.dump"
            entry["data"] = {"file": member, "ext": engine.backup["ext"], "source_db_name": backup.source_db_name,
                             "size_bytes": backup.size_bytes}
            files[member] = _path(backup)
    return files
