"""Uptime: is the app reachable the way visitors reach it?

`workers/uptime.py` asks the public address of each web app that should be running, by default every minute and at
best every 5 seconds, with the app's health check settings (path and healthy answers, see apps/health.py). The checks
are kept for 7 days; what a day adds up to is kept for 90. A number of failed checks in a row (2 unless the app says
otherwise) is an outage (notification `uptime.down`)."""
import http.client
import socket
import ssl
import time
from datetime import date, datetime, timedelta
from typing import Optional
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps import health
from core.models import UptimeCheck, UptimeDay

CHECK_SECONDS = 60                       # the default
INTERVALS = (0, 5, 10, 15, 30, 60, 120, 300, 600, 900)   # what an app may choose; 0 = no checks


def clean_interval(seconds) -> int:
    """The interval as it is stored. Raises HTTPException(400) for anything that is not on offer."""
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        value = -1
    if value not in INTERVALS:
        raise HTTPException(400, "The check interval must be off, 5, 10, 15 or 30 seconds, or 1, 2, 5, 10 or 15 minutes")
    return value


TIMEOUT_SECONDS = 10
FAILURES_FOR_OUTAGE = 2                  # the default: failed checks in a row that make an outage
FAILURES_RANGE = (1, 20)


def clean_failures(count) -> int:
    """Failed checks in a row that count as an outage, as it is stored. Raises HTTPException(400) for a bad number."""
    try:
        value = int(count)
    except (TypeError, ValueError):
        value = 0
    if not FAILURES_RANGE[0] <= value <= FAILURES_RANGE[1]:
        raise HTTPException(400, f"Failed checks for an outage must be between {FAILURES_RANGE[0]} and {FAILURES_RANGE[1]}")
    return value


def timeout_for(interval: int) -> int:
    """A check must be over before the next one is due."""
    return max(2, min(TIMEOUT_SECONDS, interval - 1)) if interval else TIMEOUT_SECONDS
RAW_DAYS = 7
DAYS = 90


def probe(url: str, path: str, expect: str, timeout: int = TIMEOUT_SECONDS) -> tuple[bool, Optional[int], str]:
    """Ask `url` once: (healthy, response time in ms, what happened). Blocking: run it with asyncio.to_thread."""
    parts = urlsplit(url)
    host, secure = parts.hostname or "", parts.scheme == "https"
    started = time.monotonic()
    try:
        if secure:
            conn = http.client.HTTPSConnection(host, parts.port or 443, timeout=timeout, context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(host, parts.port or 80, timeout=timeout)
        try:
            conn.request("GET", path or "/", headers={"User-Agent": "Cloudbase-uptime", "Connection": "close"})
            status = int(conn.getresponse().status or 0)
        finally:
            conn.close()
    except ssl.SSLError as exc:
        return False, None, f"certificate problem ({exc.reason or type(exc).__name__})"[:200]
    except socket.timeout:
        return False, None, f"no answer within {timeout}s"
    except Exception as exc:
        return False, None, f"no answer ({type(exc).__name__})"
    ms = int((time.monotonic() - started) * 1000)
    if health.is_healthy(status, expect):
        return True, ms, f"HTTP {status}"
    return False, ms, f"HTTP {status} on {path or '/'}"


async def record(db: AsyncSession, app_id: int, ok: bool, ms: Optional[int], detail: str, now: Optional[datetime] = None) -> None:
    now = now or datetime.utcnow()
    db.add(UptimeCheck(app_id=app_id, timestamp=now, ok=ok, response_ms=ms, detail=detail[:200]))
    day = (await db.execute(select(UptimeDay).where(UptimeDay.app_id == app_id, UptimeDay.day == now.date()))).scalar_one_or_none()
    if day is None:
        day = UptimeDay(app_id=app_id, day=now.date(), checks=0, ok_checks=0, total_ms=0)
        db.add(day)
    day.checks += 1
    if ok:
        day.ok_checks += 1
        day.total_ms += ms or 0
    await db.commit()


async def prune(db: AsyncSession, now: Optional[datetime] = None) -> None:
    now = now or datetime.utcnow()
    await db.execute(delete(UptimeCheck).where(UptimeCheck.timestamp < now - timedelta(days=RAW_DAYS)))
    await db.execute(delete(UptimeDay).where(UptimeDay.day < now.date() - timedelta(days=DAYS)))
    await db.commit()


def incidents(checks: list[tuple[datetime, bool]], needed: int = FAILURES_FOR_OUTAGE) -> list[dict]:
    """Outages from checks in time order: runs of at least `needed` failed checks, newest first.
    `until` is None while the outage lasts."""
    out, start, count = [], None, 0
    for ts, ok in checks:
        if not ok:
            start = start or ts
            count += 1
            continue
        if start and count >= needed:
            out.append({"from": start, "until": ts})
        start, count = None, 0
    if start and count >= needed:
        out.append({"from": start, "until": None})
    return out[::-1]


async def summary(db: AsyncSession, app_id: int, today: Optional[date] = None, needed: int = FAILURES_FOR_OUTAGE) -> dict:
    """Uptime of the last 90 days (per day and in total), the average response time and the outages of the last 7 days."""
    today = today or datetime.utcnow().date()
    first = today - timedelta(days=DAYS - 1)
    rows = {d.day: d for d in (await db.execute(
        select(UptimeDay).where(UptimeDay.app_id == app_id, UptimeDay.day >= first))).scalars()}
    days = []
    for i in range(DAYS):
        d = first + timedelta(days=i)
        row = rows.get(d)
        days.append({"day": d.isoformat(), "uptime": round(100 * row.ok_checks / row.checks, 2) if row and row.checks else None})
    checks = sum(r.checks for r in rows.values())
    ok = sum(r.ok_checks for r in rows.values())
    ms = sum(r.total_ms for r in rows.values())
    recent = (await db.execute(
        select(UptimeCheck.timestamp, UptimeCheck.ok, UptimeCheck.response_ms, UptimeCheck.detail)
        .where(UptimeCheck.app_id == app_id).order_by(UptimeCheck.timestamp))).all()
    last = recent[-1] if recent else None
    return {
        "uptime": round(100 * ok / checks, 3) if checks else None,
        "average_ms": round(ms / ok) if ok else None,
        "days": days,
        "recent_ms": [r[2] for r in recent[-60:]],   # the last hour, for the small chart; None = a failed check
        "incidents": [{"from": i["from"].isoformat() + "Z", "until": i["until"].isoformat() + "Z" if i["until"] else None}
                      for i in incidents([(r[0], r[1]) for r in recent], needed)][:20],
        "last_check": {"at": last[0].isoformat() + "Z", "ok": last[1], "response_ms": last[2], "detail": last[3]} if last else None,
    }


def state_of(app, running: bool, last_check: Optional[dict]) -> str:
    """up | down | maintenance | stopped | unknown"""
    if app.maintenance_mode:
        return "maintenance"
    if not running:
        return "stopped"
    if not last_check:
        return "unknown"
    return "up" if last_check["ok"] else "down"
