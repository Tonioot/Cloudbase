import asyncio
import logging
from datetime import timedelta
from typing import Optional
from sqlalchemy import select
from core.database import AsyncSessionLocal
from core.models import NotificationDelivery, Instance, Notification, Database, Application, Node
from nginx import certbot as nginx_certbot
from notify.emit import emit, open_keys, resolve
from notify.events import _CERT_CHECK_SECONDS, _DISK_HYSTERESIS, _WATCH_SECONDS
from notify.settings import settings
from notify.state import _utcnow, _wake

log = logging.getLogger("cloudbase.notify")


# ── Watching state ────────────────────────────────────────────────────────────

def _duration(seconds: int) -> str:
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{round(seconds / 60)} min"
    return f"{seconds / 3600:.1f} h"


_bad_since: dict[str, float] = {}  # instance/database problem key → when it was first seen


_cert_days: dict[str, tuple[int, Optional[int]]] = {}  # certificate name → (days left, app id)


_cert_checked_at = 0.0


async def _check_certificates() -> None:
    global _cert_checked_at
    async with AsyncSessionLocal() as db:
        apps = (await db.execute(select(Application.id, Application.name, Application.ssl_cert_path)
                                 .where(Application.ssl_cert_path.is_not(None)))).all()
    found: dict[str, tuple[int, Optional[int]]] = {}
    for app_id, _app_name, cert_path in apps:
        name = nginx_certbot.name_from_path(cert_path)
        if not name or name in found:
            continue
        info = await asyncio.to_thread(nginx_certbot.info, name)
        if info:
            found[name] = (info["days_left"], app_id)
    _cert_days.clear()
    _cert_days.update(found)
    _cert_checked_at = asyncio.get_running_loop().time()


async def _watch_once() -> None:
    now = _utcnow()
    cfg = await settings()
    disk_high = float(cfg["disk_percent"])
    problems: dict[str, tuple] = {}   # key → (event, title, message, app_id, link)
    resolutions: dict[str, tuple] = {}  # key → (title, message) for problems that are fixed
    gone: set[str] = set()             # keys whose subject no longer exists: close silently

    async with AsyncSessionLocal() as db:
        nodes = (await db.execute(select(Node))).scalars().all()
        instances = (await db.execute(select(Instance))).scalars().all()
        databases = (await db.execute(select(Database))).scalars().all()
        app_names = dict((await db.execute(select(Application.id, Application.name))).all())

    open_ = {}
    for prefix in ("node.offline:", "node.disk_high:", "instance.failed:", "database.down:", "cert.expiring:"):
        open_.update(await open_keys(prefix))

    node_ids = set()
    for node in nodes:
        node_ids.add(node.id)
        key = f"node.offline:{node.id}"
        if (node.enabled and not node.is_local and node.status == "offline"
                and node.offline_since and now - node.offline_since >= timedelta(minutes=cfg["node_offline_minutes"])):
            problems[key] = ("node.offline", f"Node {node.name} is offline",
                             "Its instances are out of the load balancer and come back by themselves when it reconnects. "
                             "Instances on other nodes keep serving.", None, f"/node?id={node.id}")
        elif key in open_:
            resolutions[key] = (f"Node {node.name} is back online",
                                f"It came back {_duration(open_[key])} after the alert.")
        key = f"node.disk_high:{node.id}"
        disk = node.node_disk_percent
        if disk is not None and (disk >= disk_high or (key in open_ and disk >= disk_high - _DISK_HYSTERESIS)):
            problems[key] = ("node.disk_high", f"Disk on {node.name} is {disk:.0f}% full",
                             "Builds, logs and backups stop working when the disk is full. "
                             "Cloudbase cleans up old images and build cache itself; use \"Clean up Docker\" on the node page to do it now, or add space.", None, f"/node?id={node.id}")
        elif key in open_ and disk is not None:
            resolutions[key] = (f"Disk on {node.name} is back to {disk:.0f}%", "")

    instance_ids = set()
    for r in instances:
        instance_ids.add(r.id)
        key = f"instance.failed:{r.id}"
        app_name = app_names.get(r.app_id, f"app {r.app_id}")
        if r.status == "error":
            problems[key] = ("instance.failed", f"Instance {r.id} of {app_name} has failed",
                             (r.last_error or "The container stopped and was not restarted.")[:1000],
                             r.app_id, f"/app?id={r.app_id}")
        elif key in open_ and r.status == "running":
            resolutions[key] = (f"Instance {r.id} of {app_name} is running again", "")
        elif key in open_ and r.status == "stopped":
            gone.add(key)  # stopped on purpose

    database_ids = set()
    for d in databases:
        database_ids.add(d.id)
        key = f"database.down:{d.id}"
        if d.desired_state == "running" and d.status in ("error", "unhealthy"):
            problems[key] = ("database.down", f"Database {d.name} is {d.status}",
                             (d.last_error or "")[:1000], None, f"/database?id={d.id}")
        elif key in open_ and d.status == "running":
            resolutions[key] = (f"Database {d.name} is running again",
                                f"It came back {_duration(open_[key])} after the alert.")
        elif key in open_ and d.desired_state == "stopped":
            gone.add(key)

    loop_time = asyncio.get_running_loop().time()
    if not _cert_checked_at or loop_time - _cert_checked_at > _CERT_CHECK_SECONDS:
        try:
            await _check_certificates()
        except Exception as e:
            log.debug("certificate check failed: %s", e)
    for name, (days, app_id) in _cert_days.items():
        if days <= cfg["cert_days"]:
            problems[f"cert.expiring:{name}"] = (
                "cert.expiring", f"Certificate for {name} expires in {days} day{'s' if days != 1 else ''}",
                "Let's Encrypt renews certificates 30 days before they expire, so renewal keeps failing. "
                "Check that the domain still points to this server and that port 80 is reachable.",
                app_id, f"/app?id={app_id}")
    for key in open_:
        if key.startswith("cert.expiring:") and key not in problems and _cert_checked_at:
            resolutions[key] = (f"Certificate for {key.split(':', 1)[1]} was renewed", "")

    # Subjects that were deleted
    for key in open_:
        kind, _, ident = key.partition(":")
        if ident.isdigit() and (
            (kind in ("node.offline", "node.disk_high") and int(ident) not in node_ids)
            or (kind == "instance.failed" and int(ident) not in instance_ids)
            or (kind == "database.down" and int(ident) not in database_ids)
        ):
            gone.add(key)

    for key in list(_bad_since):
        if key not in problems:
            del _bad_since[key]
    for key, (event, title, message, app_id, link) in problems.items():
        if key in open_:
            continue
        if event in ("instance.failed", "database.down"):
            first = _bad_since.setdefault(key, loop_time)
            if loop_time - first < cfg["down_minutes"] * 60:
                continue
        await emit(event, title, message, app_id=app_id, link=link, key=key)
    for key, (title, message) in resolutions.items():
        if key not in problems:
            await resolve(key, title, message)
    for key in gone - set(problems):
        await resolve(key, "", silent=True)
    if cfg["remind_hours"]:
        await _remind(cfg["remind_hours"])


async def _remind(hours: int) -> None:
    """Send problems that are still open again, to the channels that got them."""
    now = _utcnow()
    due_before = now - timedelta(hours=hours)
    async with AsyncSessionLocal() as db:
        problems = (await db.execute(
            select(Notification).where(
                Notification.state == "open", Notification.muted == False,  # noqa: E712
                Notification.severity.in_(["warning", "critical"]),
            )
        )).scalars().all()
        sent = 0
        for p in problems:
            if (p.reminded_at or p.created_at) > due_before:
                continue
            channel_ids = set((await db.execute(
                select(NotificationDelivery.channel_id).where(NotificationDelivery.notification_id == p.id)
            )).scalars().all())
            p.reminded_at = now
            if not channel_ids:
                continue
            n = Notification(
                event=p.event, severity=p.severity, title=f"Still open: {p.title}"[:300],
                message=f"Open for {_duration(int((now - p.created_at).total_seconds()))}.\n{p.message or ''}".strip()[:4000],
                app_id=p.app_id, link=p.link, dedupe_key=p.dedupe_key, resolves_id=p.id, in_inbox=False,
            )
            db.add(n)
            await db.flush()
            for channel_id in channel_ids:
                db.add(NotificationDelivery(notification_id=n.id, channel_id=channel_id))
            sent += 1
        await db.commit()
    if sent:
        log.info("reminded about %s open problem(s)", sent)
        _wake.set()


async def watcher() -> None:
    """Turn unhealthy state into problems and resolve them again (primary only)."""
    await asyncio.sleep(60)  # let nodes reconnect after a restart first
    while True:
        try:
            await _watch_once()
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.warning("[watcher] error: %s", e)
        await asyncio.sleep(_WATCH_SECONDS)
