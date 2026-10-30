"""Notifications: the inbox, channels and per-app muting."""

import json
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import auth as _auth
import notify
import notify_channels
from audit import log_audit
from database import get_db
from models import Application, Notification, NotificationChannel, NotificationDelivery, User

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

_view = _auth.require_permission("notifications.view")
_manage = _auth.require_permission("notifications.manage")


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() + "Z" if dt else None


async def _db_user(db: AsyncSession, username: str) -> Optional[User]:
    return (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()


# ── Inbox ─────────────────────────────────────────────────────────────────────

def _as_dict(n: Notification, app_names: dict) -> dict:
    return {
        "id": n.id, "event": n.event, "severity": n.severity, "title": n.title, "message": n.message,
        "app_id": n.app_id, "app_name": app_names.get(n.app_id), "link": n.link, "state": n.state,
        "muted": n.muted, "created_at": _iso(n.created_at), "resolved_at": _iso(n.resolved_at),
    }


@router.get("")
async def list_notifications(
    limit: int = 50,
    before: Optional[int] = None,
    severity: Optional[str] = None,
    state: Optional[str] = None,
    app_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(_view),
):
    q = (select(Notification).where(Notification.in_inbox == True)  # noqa: E712
         .order_by(Notification.id.desc()).limit(max(1, min(limit, 200))))
    if before:
        q = q.where(Notification.id < before)
    if severity:
        q = q.where(Notification.severity == severity)
    if state == "open":
        q = q.where(Notification.state == "open")
    elif state == "problems":
        q = q.where(Notification.severity.in_(["warning", "critical"]))
    if app_id:
        q = q.where(Notification.app_id == app_id)
    rows = (await db.execute(q)).scalars().all()
    app_names = dict((await db.execute(select(Application.id, Application.name))).all())
    me = await _db_user(db, user["username"])
    return {
        "items": [_as_dict(n, app_names) for n in rows],
        "read_at": _iso(me.notifications_read_at) if me else None,
        **(await _counts(db, me)),
    }


async def _counts(db: AsyncSession, me: Optional[User]) -> dict:
    q = select(func.count(Notification.id)).where(Notification.in_inbox == True)  # noqa: E712
    if me and me.notifications_read_at:
        q = q.where(Notification.created_at > me.notifications_read_at)
    unread = (await db.execute(q)).scalar() or 0
    open_ = (await db.execute(
        select(func.count(Notification.id)).where(Notification.state == "open", Notification.in_inbox == True)  # noqa: E712
    )).scalar() or 0
    return {"unread": unread, "open": open_}


@router.get("/summary")
async def summary(db: AsyncSession = Depends(get_db), user: dict = Depends(_view)):
    """Unread and open counts for the bell."""
    return await _counts(db, await _db_user(db, user["username"]))


@router.post("/read")
async def mark_read(db: AsyncSession = Depends(get_db), user: dict = Depends(_view)):
    me = await _db_user(db, user["username"])
    if me is None:
        raise HTTPException(404, "User not found")
    me.notifications_read_at = datetime.utcnow()
    await db.commit()
    return {"unread": 0}


# ── Catalog ───────────────────────────────────────────────────────────────────

@router.get("/catalog")
async def catalog(_user: dict = Depends(_view)):
    """Events and channel types, for the settings screen."""
    return {
        "events": [{"key": k, "label": label, "group": group, "severity": sev, "problem": k in notify.PROBLEM_EVENTS}
                   for k, (label, group, sev) in notify.EVENTS.items()],
        "default_events": notify.DEFAULT_EVENTS,
        "setting_choices": notify.SETTING_CHOICES,
        "channel_types": [
            {"type": t, "label": spec["label"], "fields": [
                {"key": k, "label": label, "kind": kind, "required": required, "help": help_}
                for k, label, kind, required, help_ in spec["fields"]
            ]}
            for t, spec in notify_channels.CHANNEL_TYPES.items()
        ],
    }


# ── Rules ─────────────────────────────────────────────────────────────────────

class SettingsRequest(BaseModel):
    inbox_events: Optional[list[str]] = None
    node_offline_minutes: Optional[int] = None
    down_minutes: Optional[int] = None
    disk_percent: Optional[int] = None
    cert_days: Optional[int] = None
    remind_hours: Optional[int] = None
    send_resolved: Optional[bool] = None


@router.get("/settings")
async def get_settings(db: AsyncSession = Depends(get_db), _user: dict = Depends(_manage)):
    return await notify.load_settings(db)


@router.put("/settings")
async def put_settings(
    req: SettingsRequest,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_manage),
    actor: str = Depends(_auth.get_current_actor),
):
    update = req.model_dump(exclude_none=True)
    unknown = [e for e in update.get("inbox_events", []) if e not in notify.EVENTS]
    if unknown:
        raise HTTPException(400, f"Unknown event '{unknown[0]}'")
    for key, value in update.items():
        if key in notify.SETTING_CHOICES and value not in notify.SETTING_CHOICES[key]:
            raise HTTPException(400, f"{key} must be one of {notify.SETTING_CHOICES[key]}")
    saved = await notify.save_settings(db, update)
    await log_audit(db, "notifications.settings", actor=actor, detail={k: v for k, v in update.items() if k != "inbox_events"})
    await db.commit()
    return saved


# ── Channels ──────────────────────────────────────────────────────────────────

class ChannelRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    type: str
    config: dict = {}
    enabled: bool = True
    events: list[str] = []
    app_ids: list[int] = []


class TestRequest(BaseModel):
    type: str
    config: dict = {}
    channel_id: Optional[int] = None  # stored secrets fill in empty fields


def _channel_dict(ch: NotificationChannel) -> dict:
    return {
        "id": ch.id, "name": ch.name, "type": ch.type, "enabled": ch.enabled,
        "events": notify._list(ch.events), "app_ids": notify._list(ch.app_ids),
        "config": notify.public_config(ch.type, notify.load_config(ch)),
        "last_status": ch.last_status, "last_error": ch.last_error, "last_sent_at": _iso(ch.last_sent_at),
    }


async def _apply(ch: NotificationChannel, req: ChannelRequest, db: AsyncSession) -> None:
    if req.type not in notify_channels.CHANNEL_TYPES:
        raise HTTPException(400, f"Unknown channel type '{req.type}'")
    if ch.id is not None and ch.type != req.type:
        raise HTTPException(400, "The type of a channel can't be changed")
    config = notify.merge_config(req.type, notify.load_config(ch) if ch.config else {}, req.config)
    error = notify_channels.validate(req.type, config)
    if error:
        raise HTTPException(400, error)
    unknown = [e for e in req.events if e not in notify.EVENTS]
    if unknown:
        raise HTTPException(400, f"Unknown event '{unknown[0]}'")
    if req.app_ids:
        found = set((await db.execute(select(Application.id).where(Application.id.in_(req.app_ids)))).scalars().all())
        if len(found) != len(set(req.app_ids)):
            raise HTTPException(400, "One of the selected apps doesn't exist")
    ch.name, ch.type, ch.enabled = req.name.strip(), req.type, req.enabled
    ch.events = json.dumps(list(dict.fromkeys(req.events)))
    ch.app_ids = json.dumps(sorted(set(req.app_ids))) if req.app_ids else None
    notify.store_config(ch, config)


@router.get("/channels")
async def list_channels(db: AsyncSession = Depends(get_db), _user: dict = Depends(_manage)):
    rows = (await db.execute(select(NotificationChannel).order_by(NotificationChannel.id))).scalars().all()
    return [_channel_dict(ch) for ch in rows]


@router.post("/channels")
async def create_channel(
    req: ChannelRequest,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_manage),
    actor: str = Depends(_auth.get_current_actor),
):
    ch = NotificationChannel(config="")
    await _apply(ch, req, db)
    db.add(ch)
    await db.flush()
    await log_audit(db, "notifications.channel_create", actor=actor,
                    detail={"channel_id": ch.id, "name": ch.name, "type": ch.type})
    await db.commit()
    return _channel_dict(ch)


@router.put("/channels/{channel_id}")
async def update_channel(
    channel_id: int,
    req: ChannelRequest,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_manage),
    actor: str = Depends(_auth.get_current_actor),
):
    ch = await db.get(NotificationChannel, channel_id)
    if ch is None:
        raise HTTPException(404, "Channel not found")
    await _apply(ch, req, db)
    await log_audit(db, "notifications.channel_update", actor=actor,
                    detail={"channel_id": ch.id, "name": ch.name, "enabled": ch.enabled})
    await db.commit()
    return _channel_dict(ch)


@router.delete("/channels/{channel_id}")
async def delete_channel(
    channel_id: int,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_manage),
    actor: str = Depends(_auth.get_current_actor),
):
    ch = await db.get(NotificationChannel, channel_id)
    if ch is None:
        raise HTTPException(404, "Channel not found")
    await log_audit(db, "notifications.channel_delete", actor=actor,
                    detail={"channel_id": ch.id, "name": ch.name, "type": ch.type})
    await db.delete(ch)
    await db.commit()
    return {"ok": True}


@router.post("/channels/test")
async def test_channel(req: TestRequest, db: AsyncSession = Depends(get_db), _user: dict = Depends(_manage)):
    """Send a test message with the settings in the form (saved or not)."""
    if req.type not in notify_channels.CHANNEL_TYPES:
        raise HTTPException(400, f"Unknown channel type '{req.type}'")
    stored = {}
    if req.channel_id:
        ch = await db.get(NotificationChannel, req.channel_id)
        if ch is not None and ch.type == req.type:
            stored = notify.load_config(ch)
    config = notify.merge_config(req.type, stored, req.config)
    error = notify_channels.validate(req.type, config)
    if error:
        raise HTTPException(400, error)
    error = await notify.send_test(req.type, config)
    return {"ok": error is None, "error": error}


# ── Delivery log ──────────────────────────────────────────────────────────────

@router.get("/deliveries")
async def list_deliveries(
    limit: int = 50,
    channel_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_manage),
):
    q = (select(NotificationDelivery, Notification, NotificationChannel)
         .join(Notification, Notification.id == NotificationDelivery.notification_id)
         .join(NotificationChannel, NotificationChannel.id == NotificationDelivery.channel_id)
         .order_by(NotificationDelivery.id.desc()).limit(max(1, min(limit, 200))))
    if channel_id:
        q = q.where(NotificationDelivery.channel_id == channel_id)
    rows = (await db.execute(q)).all()
    return [{
        "id": d.id, "status": d.status, "attempts": d.attempts, "error": d.error,
        "created_at": _iso(d.created_at), "sent_at": _iso(d.sent_at),
        "next_attempt_at": _iso(d.next_attempt_at) if d.status == "pending" else None,
        "notification": {"id": n.id, "title": n.title, "severity": n.severity, "event": n.event},
        "channel": {"id": ch.id, "name": ch.name, "type": ch.type},
    } for d, n, ch in rows]


@router.post("/deliveries/{delivery_id}/retry")
async def retry_delivery(delivery_id: int, db: AsyncSession = Depends(get_db), _user: dict = Depends(_manage)):
    d = await db.get(NotificationDelivery, delivery_id)
    if d is None:
        raise HTTPException(404, "Delivery not found")
    if d.status == "sent":
        raise HTTPException(409, "Already sent")
    d.status, d.next_attempt_at, d.attempts = "pending", datetime.utcnow(), 0
    await db.commit()
    notify.wake()
    return {"ok": True}


# ── Per app ───────────────────────────────────────────────────────────────────

class MuteRequest(BaseModel):
    minutes: Optional[int] = Field(default=None, ge=0, le=60 * 24 * 365)  # 0 / None = unmute


@router.get("/apps/{app_id}")
async def app_notifications(
    app_id: int,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("apps.view")),
):
    """Mute state and which channels get this app's events."""
    app = await db.get(Application, app_id)
    if app is None:
        raise HTTPException(404, "App not found")
    rows = (await db.execute(select(NotificationChannel).order_by(NotificationChannel.id))).scalars().all()
    app_events = {k for k, (_l, group, _s) in notify.EVENTS.items() if group in ("Deploys", "Apps", "Domains")}
    channels = []
    for ch in rows:
        scope = notify._list(ch.app_ids)
        events = [e for e in notify._list(ch.events) if e in app_events]
        if (scope and app_id not in scope) or not events:
            continue
        channels.append({"id": ch.id, "name": ch.name, "type": ch.type, "enabled": ch.enabled,
                         "events": events, "only_this_app": bool(scope)})
    muted = app.notifications_muted_until and app.notifications_muted_until > datetime.utcnow()
    return {"muted_until": _iso(app.notifications_muted_until) if muted else None, "channels": channels}


@router.put("/apps/{app_id}/mute")
async def mute_app(
    app_id: int,
    req: MuteRequest,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("apps.configure")),
    actor: str = Depends(_auth.get_current_actor),
):
    app = await db.get(Application, app_id)
    if app is None:
        raise HTTPException(404, "App not found")
    app.notifications_muted_until = (datetime.utcnow() + timedelta(minutes=req.minutes)) if req.minutes else None
    await log_audit(db, "app.notifications_mute" if req.minutes else "app.notifications_unmute",
                    actor=actor, app_id=app_id, detail={"minutes": req.minutes})
    await db.commit()
    return {"muted_until": _iso(app.notifications_muted_until)}
