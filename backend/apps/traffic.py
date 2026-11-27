"""What apps send to the public internet, added up per app per day (UTC), for billing.

Two kinds, counted where they happen:
  internet  what an app's containers send to public addresses themselves: requests to other services and the answers
            to them (containers/traffic.py, on every machine; a node reports its numbers to the primary)
  visitors  what nginx on the primary sends back to the people visiting the app's domains (nginx/traffic_logs.py)
A database counts what its public port sends to the people connected to it (dbs/proxy.py).

Traffic that stays inside (to nginx, to a database, to another app or node, to the local network) is not counted.
"""
import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import AsyncSessionLocal
from core.models import Application, AppTraffic, Database, DatabaseTraffic

log = logging.getLogger("cloudbase.traffic")

KINDS = {"internet": "internet_bytes", "visitors": "visitor_bytes"}
MAX_DAYS = 400          # asked for at once


def today() -> date:
    return datetime.now(timezone.utc).date()


def period(start: date | None, end: date | None, days: int) -> tuple[date, date]:
    """From start to end (inclusive, UTC days); without them the last `days` days up to today. ValueError when it
    is not a period that can be asked for."""
    end = end or today()
    start = start or end - timedelta(days=days - 1)
    if start > end:
        raise ValueError("start is after end")
    if (end - start).days >= MAX_DAYS:
        raise ValueError(f"At most {MAX_DAYS} days at a time")
    return start, end


async def add_in(db: AsyncSession, kind: str, counts: dict[int, int], day: date | None = None) -> None:
    """Add {app id: bytes} to today's row of each app, in the caller's transaction."""
    column = KINDS[kind]
    day = day or today()
    counts = {int(a): int(n) for a, n in (counts or {}).items() if int(n) > 0}
    if not counts:
        return
    rows = {r.app_id: r for r in (await db.execute(
        select(AppTraffic).where(AppTraffic.day == day, AppTraffic.app_id.in_(counts)))).scalars()}
    for app_id, n in counts.items():
        row = rows.get(app_id)
        if row is None:
            row = AppTraffic(app_id=app_id, day=day, internet_bytes=0, visitor_bytes=0)
            db.add(row)
        setattr(row, column, (getattr(row, column) or 0) + n)


async def add(kind: str, counts: dict[int, int]) -> None:
    if not any(int(n) > 0 for n in (counts or {}).values()):
        return
    async with AsyncSessionLocal() as db:
        await add_in(db, kind, counts)
        await db.commit()


def _as_dict(internet: int, visitors: int) -> dict:
    internet, visitors = int(internet or 0), int(visitors or 0)
    return {"internet_bytes": internet, "visitor_bytes": visitors, "total_bytes": internet + visitors}


async def app_days(db: AsyncSession, app_id: int, start: date, end: date) -> list[dict]:
    """One entry per day from start to end (inclusive), oldest first; days without traffic are 0."""
    rows = {r.day: r for r in (await db.execute(
        select(AppTraffic).where(AppTraffic.app_id == app_id, AppTraffic.day >= start, AppTraffic.day <= end)
    )).scalars()}
    out, day = [], start
    while day <= end:
        row = rows.get(day)
        out.append({"day": day.isoformat(), **_as_dict(row.internet_bytes if row else 0, row.visitor_bytes if row else 0)})
        day += timedelta(days=1)
    return out


async def per_app(db: AsyncSession, start: date, end: date, app_ids: list[int] | None = None) -> list[dict]:
    """Totals per app from start to end (inclusive), the biggest first. Deleted apps are included (name None)."""
    query = (select(AppTraffic.app_id, func.sum(AppTraffic.internet_bytes), func.sum(AppTraffic.visitor_bytes))
             .where(AppTraffic.day >= start, AppTraffic.day <= end).group_by(AppTraffic.app_id))
    if app_ids is not None:
        query = query.where(AppTraffic.app_id.in_(app_ids))
    totals = (await db.execute(query)).all()
    names = dict((await db.execute(select(Application.id, Application.name)
                                   .where(Application.id.in_([t[0] for t in totals])))).all()) if totals else {}
    out = [{"app_id": app_id, "app_name": names.get(app_id), **_as_dict(internet, visitors)}
           for app_id, internet, visitors in totals]
    return sorted(out, key=lambda r: -r["total_bytes"])


async def add_databases_in(db: AsyncSession, counts: dict[int, int], day: date | None = None) -> None:
    """Add {database id: bytes} to today's row of each database, in the caller's transaction."""
    day = day or today()
    counts = {int(d): int(n) for d, n in (counts or {}).items() if int(n) > 0}
    if not counts:
        return
    rows = {r.database_id: r for r in (await db.execute(
        select(DatabaseTraffic).where(DatabaseTraffic.day == day, DatabaseTraffic.database_id.in_(counts)))).scalars()}
    for database_id, n in counts.items():
        row = rows.get(database_id)
        if row is None:
            row = DatabaseTraffic(database_id=database_id, day=day, internet_bytes=0)
            db.add(row)
        row.internet_bytes = (row.internet_bytes or 0) + n


async def per_database(db: AsyncSession, start: date, end: date, database_ids: list[int] | None = None) -> list[dict]:
    """Totals per database from start to end (inclusive), the biggest first. Deleted ones are included (name None)."""
    query = (select(DatabaseTraffic.database_id, func.sum(DatabaseTraffic.internet_bytes))
             .where(DatabaseTraffic.day >= start, DatabaseTraffic.day <= end).group_by(DatabaseTraffic.database_id))
    if database_ids is not None:
        query = query.where(DatabaseTraffic.database_id.in_(database_ids))
    totals = (await db.execute(query)).all()
    names = dict((await db.execute(select(Database.id, Database.name)
                                   .where(Database.id.in_([t[0] for t in totals])))).all()) if totals else {}
    out = [{"database_id": database_id, "database_name": names.get(database_id), "internet_bytes": int(n or 0),
            "total_bytes": int(n or 0)} for database_id, n in totals]
    return sorted(out, key=lambda r: -r["total_bytes"])
