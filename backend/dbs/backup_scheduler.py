import asyncio
import logging
from datetime import timedelta, datetime, timezone
from typing import Optional
from sqlalchemy import select
from dbs import engines as db_engines, state as dbs_state
from core.database import AsyncSessionLocal
from core.models import DatabaseBackup, Database
from dbs.backups import _apply_retention, _offsite_catch_up, run_backup

log = logging.getLogger("cloudbase.backups")


SCHEDULER_INTERVAL_SECONDS = 60


FAILED_RETRY_SECONDS = 3600


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
            if database.id in dbs_state._busy or not db_engines.get_engine(database.engine).backup:
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
        if database_id in dbs_state._busy:
            continue
        dbs_state._busy.add(database_id)
        dbs_state._operation[database_id] = "Backing up…"
        try:
            await run_backup(database_id, "scheduled", "scheduler")
            await _apply_retention(database_id)
        except Exception:
            pass
        finally:
            dbs_state._busy.discard(database_id)
            dbs_state._operation.pop(database_id, None)


async def backup_scheduler() -> None:
    """Run due scheduled backups (primary only)."""
    await asyncio.sleep(90)
    while True:
        try:
            await _scheduler_tick()
            await _offsite_catch_up()
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.warning("[backup-scheduler] error: %s", e)
        await asyncio.sleep(SCHEDULER_INTERVAL_SECONDS)
