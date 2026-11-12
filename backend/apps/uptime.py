"""Uptime: is the app reachable the way visitors reach it?

Every minute `workers/uptime.py` asks the public address of each web app that should be running, with the app's
health check settings (path and healthy answers, see apps/health.py). The checks are kept for 7 days; what a day adds
up to is kept for 90. Two failed checks in a row are an outage (notification `uptime.down`)."""
import http.client
import socket
import ssl
import time
from datetime import date, datetime, timedelta
from typing import Optional
from urllib.parse import urlsplit

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps import health
from core.models import UptimeCheck, UptimeDay

CHECK_SECONDS = 60
TIMEOUT_SECONDS = 10
FAILURES_FOR_OUTAGE = 2
RAW_DAYS = 7
DAYS = 90


def probe(url: str, path: str, expect: str) -> tuple[bool, Optional[int], str]:
    """Ask `url` once: (healthy, response time in ms, what happened). Blocking: run it with asyncio.to_thread."""
    parts = urlsplit(url)
    host, secure = parts.hostname or "", parts.scheme == "https"
    started = time.monotonic()
    try:
        if secure:
            conn = http.client.HTTPSConnection(host, parts.port or 443, timeout=TIMEOUT_SECONDS, context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(host, parts.port or 80, timeout=TIMEOUT_SECONDS)
        try:
            conn.request("GET", path or "/", headers={"User-Agent": "Cloudbase-uptime", "Connection": "close"})
            status = int(conn.getresponse().status or 0)
        finally:
            conn.close()
    except ssl.SSLError as exc:
        return False, None, f"certificate problem ({exc.reason or type(exc).__name__})"[:200]
    except socket.timeout:
        return False, None, f"no answer within {TIMEOUT_SECONDS}s"
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


def incidents(checks: list[tuple[datetime, bool]]) -> list[dict]:
    """Outages from checks in time order: runs of at least FAILURES_FOR_OUTAGE failed checks, newest first.
    `until` is None while the outage lasts."""
    out, start, count = [], None, 0
    for ts, ok in checks:
        if not ok:
            start = start or ts
            count += 1
            continue
        if start and count >= FAILURES_FOR_OUTAGE:
            out.append({"from": start, "until": ts})
        start, count = None, 0
    if start and count >= FAILURES_FOR_OUTAGE:
        out.append({"from": start, "until": None})
    return out[::-1]


async def summary(db: AsyncSession, app_id: int, today: Optional[date] = None) -> dict:
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
        "incidents": [{"from": i["from"].isoformat() + "Z", "until": i["until"].isoformat() + "Z" if i["until"] else None}
                      for i in incidents([(r[0], r[1]) for r in recent])][:20],
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
