"""Check the public address of every web app that should be running, once a minute (primary only)."""
import asyncio
import logging
import time

from sqlalchemy import select

from apps import health, uptime as apps_uptime
from apps.serializers import public_url
from core.database import AsyncSessionLocal
from core.models import Application, Instance
from notify.emit import emit, open_keys, resolve

log = logging.getLogger("cloudbase.uptime")

_failures: dict[int, int] = {}      # app id → failed checks in a row
_last_check: dict[int, float] = {}  # app id → when it was last asked (monotonic)
_TICK_SECONDS = 10                  # the shortest interval is 30 s: look at what is due this often
_PRUNE_SECONDS = 6 * 3600
_CONCURRENT = 8


async def _targets() -> list[tuple]:
    """(app id, name, url, path, expect, interval) of the apps to check."""
    async with AsyncSessionLocal() as db:
        running = set((await db.execute(select(Instance.app_id).where(Instance.status == "running"))).scalars())
        apps = (await db.execute(select(Application).where(Application.cron_schedule.is_(None), Application.no_web == False))).scalars().all()  # noqa: E712
        out = []
        for app in apps:
            url = public_url(app)
            if app.id in running and url and not app.maintenance_mode:
                path, expect, _ = health.settings_of(app)
                path, expect = app.uptime_path or path, app.uptime_expect or expect   # its own, or the startup check's
                interval = 60 if app.uptime_interval_seconds is None else app.uptime_interval_seconds
                if interval:
                    out.append((app.id, app.name, url, path or "/", expect, interval))
        return out


async def check_app(app_id: int, name: str, url: str, path: str, expect: str, interval: int = 60) -> None:
    ok, ms, detail = await asyncio.to_thread(apps_uptime.probe, url, path, expect)
    async with AsyncSessionLocal() as db:
        await apps_uptime.record(db, app_id, ok, ms, detail)
    key = f"uptime.down:{app_id}"
    if ok:
        _failures.pop(app_id, None)
        await resolve(key, f"{name} is reachable again", "")
        return
    _failures[app_id] = _failures.get(app_id, 0) + 1
    if _failures[app_id] >= apps_uptime.FAILURES_FOR_OUTAGE:
        await emit("uptime.down", f"{name} is not reachable", f"{url}{path if path != '/' else ''}: {detail}",
                   app_id=app_id, link=f"/app?id={app_id}", key=key)


async def check_all() -> None:
    targets = await _targets()
    live = {t[0] for t in targets}
    for app_id in list(_failures):
        if app_id not in live:
            del _failures[app_id]
    for app_id in list(_last_check):
        if app_id not in live:
            del _last_check[app_id]
    # An app that stopped, went into maintenance or was deleted has nothing to be reachable: close its alert quietly
    for key in await open_keys("uptime.down:"):
        if int(key.split(":", 1)[1]) not in live:
            await resolve(key, "", silent=True)
    sem = asyncio.Semaphore(_CONCURRENT)

    now = time.monotonic()
    due = [t for t in targets if now - _last_check.get(t[0], 0.0) >= t[5] - 1]   # every app at its own pace
    for t in due:
        _last_check[t[0]] = now

    async def one(t):
        async with sem:
            try:
                await check_app(*t)
            except Exception as exc:
                log.warning("uptime check of app %s failed: %s", t[0], exc)
    await asyncio.gather(*(one(t) for t in due))


async def uptime_monitor() -> None:
    await asyncio.sleep(45)
    last_prune = 0.0
    while True:
        started = time.monotonic()
        try:
            await check_all()
            if started - last_prune > _PRUNE_SECONDS:
                async with AsyncSessionLocal() as db:
                    await apps_uptime.prune(db)
                last_prune = started
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.warning("uptime monitor: %s", exc)
        await asyncio.sleep(max(2.0, _TICK_SECONDS - (time.monotonic() - started)))
