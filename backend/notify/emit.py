import logging
from typing import Optional
from sqlalchemy import select
from core.database import AsyncSessionLocal
from core.models import Notification, NotificationDelivery, NotificationChannel, Application
from notify.config import _list
from notify.events import EVENTS
from notify.settings import settings
from notify.state import _utcnow, _wake

log = logging.getLogger("cloudbase.notify")


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
            in_inbox = event in (await settings())["inbox_events"]
            targets = [] if muted else await _channels_for(db, event, app_id)
            if not key and not in_inbox and not targets:
                return None  # nobody wants it (problems are still recorded, to know when they clear)
            n = Notification(
                event=event, severity=severity or EVENTS.get(event, ("", "", "info"))[2],
                title=title[:300], message=(message or "")[:4000], app_id=app_id, link=link,
                dedupe_key=key, state="open" if key else None, muted=muted, in_inbox=in_inbox,
            )
            db.add(n)
            await db.flush()
            for ch in targets:
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
            if silent or not (await settings())["send_resolved"]:
                await db.commit()
                return
            n = Notification(
                event=problem.event, severity="resolved", title=title[:300], message=(message or "")[:4000],
                app_id=problem.app_id, link=problem.link, dedupe_key=key, state="resolved",
                resolves_id=problem.id, muted=problem.muted, resolved_at=now, in_inbox=problem.in_inbox,
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
