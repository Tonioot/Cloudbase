import asyncio
import logging
from datetime import timedelta, timezone, datetime
from typing import Optional
from sqlalchemy import delete, select
from notify import channels
from core import system_config
from core.database import AsyncSessionLocal
from core.models import NotificationDelivery, Notification, NotificationChannel, Application
from notify.config import _scrub, load_config
from notify.events import EVENTS, RETENTION_DAYS, _BACKOFF, _DISPATCH_IDLE_SECONDS
from notify.state import _utcnow, _wake

log = logging.getLogger("cloudbase.notify")


# ── Delivering ────────────────────────────────────────────────────────────────

def _absolute(link: Optional[str]) -> Optional[str]:
    domain = system_config.get_panel_https_domain()
    if not link or not domain:
        return None
    return f"https://{domain}{link}"


# Public copy of the Cloudbase mark for chat apps; the panel serves its own once it has a domain.
_LOGO_FALLBACK = "https://raw.githubusercontent.com/Tonioot/Cloudbase/main/backend/assets/cloudbase-mark.png"


def _logo_url() -> str:
    return _absolute("/assets/cloudbase-mark.png") or _LOGO_FALLBACK


def build_message(n: Notification, app_name: Optional[str], resolves_key: Optional[str] = None) -> channels.Message:
    return channels.Message(
        id=n.id, event=n.event, severity=n.severity, title=n.title, message=n.message or "",
        url=_absolute(n.link), app=app_name,
        dedupe_key=n.dedupe_key or resolves_key or f"cloudbase-{n.id}",
        resolve=n.severity == "resolved",
        timestamp=n.created_at.replace(tzinfo=timezone.utc).isoformat(),
        event_label=EVENTS.get(n.event, (n.event,))[0], resolves_id=n.resolves_id,
        settings_url=_absolute("/settings?s=notifications"), logo_url=_logo_url(),
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
        event_label="Test", settings_url=_absolute("/settings?s=notifications"), logo_url=_logo_url(),
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
