"""Notifications: one place where Cloudbase reports what happened.

emit() stores the event in the inbox and queues a delivery per channel that
wants it; dispatcher() sends the queue in the background with retries, so a
slow Slack never holds up a deploy. Problems carry a dedupe_key: while one is
open the same problem isn't reported again, and resolve() closes it (and sends
the all-clear to the channels that got the alert).

watcher() turns state into problems — nodes offline, instances and databases
down, disks filling up, certificates not renewing — and resolves them when
the state recovers. Events that happen once (deploys, backups) are emitted
where they happen.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, select

import notify_channels as channels
import system_config
from database import AsyncSessionLocal
from env_crypto import decrypt_text, encrypt_text
from models import (Application, ApplicationReplica, Database, Node, Notification,
                    NotificationChannel, NotificationDelivery)

log = logging.getLogger("cloudbase.notify")

# key: (label, group, severity). The order is the order in the panel.
EVENTS: dict[str, tuple[str, str, str]] = {
    "deploy.failed":          ("Deploy failed",                "Deploys",   "critical"),
    "deploy.rolled_back":     ("New version rolled back",      "Deploys",   "warning"),
    "deploy.succeeded":       ("Deploy succeeded",             "Deploys",   "info"),
    "instance.failed":        ("Instance crashed or failed",   "Apps",      "warning"),
    "node.offline":           ("Node offline",                 "Nodes",     "critical"),
    "node.disk_high":         ("Disk almost full",             "Nodes",     "warning"),
    "database.down":          ("Database down",                "Databases", "critical"),
    "backup.failed":          ("Backup failed",                "Databases", "critical"),
    "backup.offsite_failed":  ("Off-site upload failed",       "Databases", "warning"),
    "cert.expiring":          ("Certificate not renewing",     "Domains",   "warning"),
}
# Sensible defaults for a new channel
DEFAULT_EVENTS = ["deploy.failed", "deploy.rolled_back", "instance.failed", "node.offline",
                  "node.disk_high", "database.down", "backup.failed", "backup.offsite_failed", "cert.expiring"]

RETENTION_DAYS = 30
_BACKOFF = [30, 120, 600, 1800, 3600]   # seconds before attempt 2, 3, …; then give up
_DISPATCH_IDLE_SECONDS = 10
_WATCH_SECONDS = 30
_NODE_OFFLINE_GRACE = timedelta(seconds=60)
_PROBLEM_GRACE_SECONDS = 60            # instances and databases must stay down this long (not a blip mid-deploy)
_DISK_HIGH, _DISK_OK = 90.0, 85.0
_CERT_WARN_DAYS = 14                    # certbot renews at 30 days left; 14 means it keeps failing
_CERT_CHECK_SECONDS = 6 * 3600

_wake = asyncio.Event()


def _utcnow() -> datetime:
    return datetime.utcnow()


# ── Channel config ────────────────────────────────────────────────────────────

def load_config(channel: NotificationChannel) -> dict:
    try:
        return json.loads(decrypt_text(channel.config, fallback_plaintext=False) or "{}")
    except ValueError:
        return {}


def store_config(channel: NotificationChannel, config: dict) -> None:
    channel.config = encrypt_text(json.dumps(config))


def merge_config(channel_type: str, current: dict, update: dict) -> dict:
    """Panel input over the stored config; an empty secret keeps the stored one."""
    merged = {}
    secrets = channels.secret_fields(channel_type)
    for key, *_rest in channels.CHANNEL_TYPES[channel_type]["fields"]:
        value = str(update.get(key) if update.get(key) is not None else "").strip()
        if key in secrets and not value:
            value = current.get(key) or ""
        merged[key] = value
    return merged


def public_config(channel_type: str, config: dict) -> dict:
    """The config for the panel: secrets only as 'set' or not."""
    secrets = channels.secret_fields(channel_type)
    return {k: ("" if k in secrets else v) for k, v in config.items()} | {
        "_secrets_set": sorted(k for k in secrets if config.get(k)),
    }


def _list(value: Optional[str]) -> list:
    try:
        return json.loads(value) if value else []
    except ValueError:
        return []


def _scrub(error: str, config: dict, channel_type: str) -> str:
    """Keep tokens and webhook URLs out of stored error messages."""
    for key in channels.secret_fields(channel_type):
        secret = config.get(key)
        if secret and len(secret) > 4:
            error = error.replace(secret, "••••")
    return error[:1000]


# ── Emitting ──────────────────────────────────────────────────────────────────

async def _channels_for(db, event: str, app_id: Optional[int]) -> list[NotificationChannel]:
    rows = (await db.execute(select(NotificationChannel).where(NotificationChannel.enabled == True))).scalars().all()  # noqa: E712
    wanted = []
    for ch in rows:
        if event not in _list(ch.events):
            continue
        scope = _list(ch.app_ids)
        if scope and app_id not in scope:
            continue  # a channel for certain apps only gets their events
        wanted.append(ch)
    return wanted


async def _is_muted(db, app_id: Optional[int]) -> bool:
    if app_id is None:
        return False
    app = await db.get(Application, app_id)
    return bool(app and app.notifications_muted_until and app.notifications_muted_until > _utcnow())


async def emit(
    event: str,
    title: str,
    message: str = "",
    *,
    app_id: Optional[int] = None,
    link: Optional[str] = None,
    key: Optional[str] = None,
    severity: Optional[str] = None,
) -> Optional[int]:
    """Record an event and queue it for every channel that wants it.

    With `key` it is a problem: nothing new is sent while a problem with that
    key is still open. Never raises — notifications must not break operations.
    """
    try:
        async with AsyncSessionLocal() as db:
            if key:
                open_ = (await db.execute(
                    select(Notification.id).where(Notification.dedupe_key == key, Notification.state == "open").limit(1)
                )).first()
                if open_:
                    return None
            muted = await _is_muted(db, app_id)
            n = Notification(
                event=event, severity=severity or EVENTS.get(event, ("", "", "info"))[2],
                title=title[:300], message=(message or "")[:4000], app_id=app_id, link=link,
                dedupe_key=key, state="open" if key else None, muted=muted,
            )
            db.add(n)
            await db.flush()
            if not muted:
                for ch in await _channels_for(db, event, app_id):
                    db.add(NotificationDelivery(notification_id=n.id, channel_id=ch.id))
            await db.commit()
            log.info("%s: %s", event, title, extra={"app_id": app_id} if app_id else None)
            _wake.set()
            return n.id
    except Exception:
        log.warning("could not record notification %s", event, exc_info=True)
        return None


async def resolve(key: str, title: str, message: str = "", *, silent: bool = False) -> None:
    """Close the open problem with `key`. Unless silent, the channels that got
    the alert also get the all-clear (PagerDuty closes its incident)."""
    try:
        async with AsyncSessionLocal() as db:
            problem = (await db.execute(
                select(Notification).where(Notification.dedupe_key == key, Notification.state == "open")
                .order_by(Notification.id.desc()).limit(1)
            )).scalar_one_or_none()
            if problem is None:
                return
            now = _utcnow()
            problem.state, problem.resolved_at = "resolved", now
            if silent:
                await db.commit()
                return
            n = Notification(
                event=problem.event, severity="resolved", title=title[:300], message=(message or "")[:4000],
                app_id=problem.app_id, link=problem.link, dedupe_key=key, state="resolved",
                resolves_id=problem.id, muted=problem.muted, resolved_at=now,
            )
            db.add(n)
            await db.flush()
            # Exactly the channels that were told about the problem
            sent_to = (await db.execute(
                select(NotificationDelivery.channel_id).where(NotificationDelivery.notification_id == problem.id)
            )).scalars().all()
            for channel_id in set(sent_to):
                db.add(NotificationDelivery(notification_id=n.id, channel_id=channel_id))
            await db.commit()
            log.info("resolved: %s", title, extra={"app_id": problem.app_id} if problem.app_id else None)
            _wake.set()
    except Exception:
        log.warning("could not resolve notification %s", key, exc_info=True)


async def open_keys(prefix: str) -> dict[str, int]:
    """Open problems whose key starts with `prefix`: {key: created_at age in s}."""
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Notification.dedupe_key, Notification.created_at)
            .where(Notification.state == "open", Notification.dedupe_key.like(f"{prefix}%"))
        )).all()
    now = _utcnow()
    return {k: int((now - created).total_seconds()) for k, created in rows}


# ── Delivering ────────────────────────────────────────────────────────────────

def _absolute(link: Optional[str]) -> Optional[str]:
    domain = system_config.get_panel_https_domain()
    if not link or not domain:
        return None
    return f"https://{domain}{link}"


def build_message(n: Notification, app_name: Optional[str], resolves_key: Optional[str] = None) -> channels.Message:
    return channels.Message(
        id=n.id, event=n.event, severity=n.severity, title=n.title, message=n.message or "",
        url=_absolute(n.link), app=app_name,
        dedupe_key=n.dedupe_key or resolves_key or f"cloudbase-{n.id}",
        resolve=n.severity == "resolved",
        timestamp=n.created_at.replace(tzinfo=timezone.utc).isoformat(),
    )


async def _deliver(delivery_id: int) -> None:
    async with AsyncSessionLocal() as db:
        d = await db.get(NotificationDelivery, delivery_id)
        if d is None or d.status != "pending":
            return
        n = await db.get(Notification, d.notification_id)
        ch = await db.get(NotificationChannel, d.channel_id)
        if n is None or ch is None:
            await db.delete(d)
            await db.commit()
            return
        app = await db.get(Application, n.app_id) if n.app_id else None
        config = load_config(ch)
        msg = build_message(n, app.name if app else None)
        ch_type, ch_enabled = ch.type, ch.enabled

    error = None
    if not ch_enabled:
        error = "Channel is turned off"
    else:
        try:
            await channels.send(ch_type, config, msg)
        except asyncio.TimeoutError:
            error = "Timed out"
        except Exception as e:
            error = _scrub(str(e) or type(e).__name__, config, ch_type)

    async with AsyncSessionLocal() as db:
        d = await db.get(NotificationDelivery, delivery_id)
        ch = await db.get(NotificationChannel, d.channel_id) if d else None
        if d is None:
            return
        now = _utcnow()
        d.attempts += 1
        if error is None:
            d.status, d.sent_at, d.error = "sent", now, None
        else:
            d.error = error
            if d.attempts > len(_BACKOFF) or not ch_enabled:
                d.status = "failed"
            else:
                d.next_attempt_at = now + timedelta(seconds=_BACKOFF[d.attempts - 1])
            log.warning("delivery %s to channel %s failed (attempt %s): %s", d.id, d.channel_id, d.attempts, error)
        if ch is not None:
            ch.last_status, ch.last_error = ("sent", None) if error is None else ("failed", error)
            if error is None:
                ch.last_sent_at = now
        await db.commit()


async def _dispatch_once() -> int:
    async with AsyncSessionLocal() as db:
        due = (await db.execute(
            select(NotificationDelivery.id)
            .where(NotificationDelivery.status == "pending", NotificationDelivery.next_attempt_at <= _utcnow())
            .order_by(NotificationDelivery.id).limit(25)
        )).scalars().all()
    if due:
        await asyncio.gather(*(_deliver(i) for i in due), return_exceptions=True)
    return len(due)


async def _cleanup() -> None:
    cutoff = _utcnow() - timedelta(days=RETENTION_DAYS)
    async with AsyncSessionLocal() as db:
        await db.execute(delete(Notification).where(
            Notification.created_at < cutoff, (Notification.state != "open") | (Notification.state.is_(None)),
        ))
        await db.commit()


async def dispatcher() -> None:
    """Send queued deliveries (primary only)."""
    await asyncio.sleep(5)
    last_cleanup = 0.0
    loop = asyncio.get_running_loop()
    while True:
        try:
            sent = await _dispatch_once()
            if loop.time() - last_cleanup > 3600:
                last_cleanup = loop.time()
                await _cleanup()
            if sent:
                continue
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.warning("[dispatcher] error: %s", e)
        _wake.clear()
        try:
            await asyncio.wait_for(_wake.wait(), timeout=_DISPATCH_IDLE_SECONDS)
        except asyncio.TimeoutError:
            pass


async def send_test(channel_type: str, config: dict) -> Optional[str]:
    """Send a test message right away. Returns an error message or None."""
    msg = channels.Message(
        id=0, event="test", severity="info", title="Test notification from Cloudbase",
        message="If you can read this, the channel works.", url=_absolute("/settings?s=notifications"),
        app=None, dedupe_key="cloudbase-test", resolve=False,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
    try:
        await channels.send(channel_type, config, msg)
        return None
    except asyncio.TimeoutError:
        return "Timed out"
    except Exception as e:
        return _scrub(str(e) or type(e).__name__, config, channel_type)


def wake() -> None:
    _wake.set()


# ── Watching state ────────────────────────────────────────────────────────────

def _duration(seconds: int) -> str:
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{round(seconds / 60)} min"
    return f"{seconds / 3600:.1f} h"


_bad_since: dict[str, float] = {}  # instance/database problem key → when it was first seen


_cert_problems: dict[str, tuple[str, str, Optional[int], str]] = {}  # key → (title, message, app_id, link)
_cert_checked_at = 0.0


async def _check_certificates() -> None:
    import certificates
    global _cert_checked_at
    async with AsyncSessionLocal() as db:
        apps = (await db.execute(select(Application.id, Application.name, Application.ssl_cert_path)
                                 .where(Application.ssl_cert_path.is_not(None)))).all()
    found: dict[str, tuple[str, str, Optional[int], str]] = {}
    seen: set[str] = set()
    for app_id, app_name, cert_path in apps:
        name = certificates.name_from_path(cert_path)
        if not name or name in seen:
            continue
        seen.add(name)
        info = await asyncio.to_thread(certificates.info, name)
        if info and info["days_left"] <= _CERT_WARN_DAYS:
            days = info["days_left"]
            found[f"cert.expiring:{name}"] = (
                f"Certificate for {name} expires in {days} day{'s' if days != 1 else ''}",
                "Let's Encrypt renews certificates 30 days before they expire, so renewal keeps failing. "
                "Check that the domain still points to this server and that port 80 is reachable.",
                app_id, f"/app?id={app_id}",
            )
    _cert_problems.clear()
    _cert_problems.update(found)
    _cert_checked_at = asyncio.get_running_loop().time()


async def _watch_once() -> None:
    now = _utcnow()
    problems: dict[str, tuple] = {}   # key → (event, title, message, app_id, link)
    resolutions: dict[str, tuple] = {}  # key → (title, message) for problems that are fixed
    gone: set[str] = set()             # keys whose subject no longer exists: close silently

    async with AsyncSessionLocal() as db:
        nodes = (await db.execute(select(Node))).scalars().all()
        replicas = (await db.execute(select(ApplicationReplica))).scalars().all()
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
                and node.offline_since and now - node.offline_since >= _NODE_OFFLINE_GRACE):
            problems[key] = ("node.offline", f"Node {node.name} is offline",
                             "Its instances are out of the load balancer and come back by themselves when it reconnects. "
                             "Instances on other nodes keep serving.", None, f"/node?id={node.id}")
        elif key in open_:
            resolutions[key] = (f"Node {node.name} is back online",
                                f"It came back {_duration(open_[key])} after the alert.")
        key = f"node.disk_high:{node.id}"
        disk = node.node_disk_percent
        if disk is not None and (disk >= _DISK_HIGH or (key in open_ and disk >= _DISK_OK)):
            problems[key] = ("node.disk_high", f"Disk on {node.name} is {disk:.0f}% full",
                             "Builds, logs and backups stop working when the disk is full. "
                             "Clean up old Docker images (docker system prune) or add space.", None, f"/node?id={node.id}")
        elif key in open_ and disk is not None:
            resolutions[key] = (f"Disk on {node.name} is back to {disk:.0f}%", "")

    replica_ids = set()
    for r in replicas:
        replica_ids.add(r.id)
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
    for key, (title, message, app_id, link) in _cert_problems.items():
        problems[key] = ("cert.expiring", title, message, app_id, link)
    for key in open_:
        if key.startswith("cert.expiring:") and key not in _cert_problems and _cert_checked_at:
            resolutions[key] = (f"Certificate for {key.split(':', 1)[1]} was renewed", "")

    # Subjects that were deleted
    for key in open_:
        kind, _, ident = key.partition(":")
        if ident.isdigit() and (
            (kind in ("node.offline", "node.disk_high") and int(ident) not in node_ids)
            or (kind == "instance.failed" and int(ident) not in replica_ids)
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
            if loop_time - first < _PROBLEM_GRACE_SECONDS:
                continue
        await emit(event, title, message, app_id=app_id, link=link, key=key)
    for key, (title, message) in resolutions.items():
        if key not in problems:
            await resolve(key, title, message)
    for key in gone - set(problems):
        await resolve(key, "", silent=True)


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
