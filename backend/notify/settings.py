import json
from typing import Optional
from sqlalchemy import select
from core.database import AsyncSessionLocal
from core.models import SystemConfig
from notify.events import EVENTS

# ── Settings (Settings → Notifications → Rules and the In-app column) ─────────

SETTINGS_KEY = "notifications.settings"


DEFAULT_SETTINGS = {
    "inbox_events": list(EVENTS),     # shown under the bell
    "node_offline_minutes": 1,        # a node is reported after being offline this long
    "down_minutes": 1,                # instances and databases: not a blip mid-deploy
    "disk_percent": 90,
    "cert_days": 14,                  # certbot renews at 30 days left; 14 means it keeps failing
    "remind_hours": 0,                # repeat open problems to channels; 0 = never
    "send_resolved": True,            # send the all-clear when a problem is fixed
}


SETTING_CHOICES = {
    "node_offline_minutes": [1, 2, 5, 10, 15, 30],
    "down_minutes": [1, 2, 5, 10, 15],
    "disk_percent": [80, 85, 90, 95],
    "cert_days": [7, 14, 21],
    "remind_hours": [0, 1, 4, 12, 24],
}


_settings: Optional[dict] = None


def _normalize_settings(raw: dict) -> dict:
    merged = {**DEFAULT_SETTINGS, **{k: v for k, v in (raw or {}).items() if k in DEFAULT_SETTINGS}}
    known = set((raw or {}).get("known_events") or EVENTS)  # events that existed when this was saved
    wanted = set(merged["inbox_events"] or []) | {e for e in EVENTS if e not in known}
    merged["inbox_events"] = [e for e in EVENTS if e in wanted]
    for key, choices in SETTING_CHOICES.items():
        if merged[key] not in choices:
            merged[key] = DEFAULT_SETTINGS[key]
    merged["send_resolved"] = bool(merged["send_resolved"])
    return merged


async def load_settings(db=None) -> dict:
    global _settings
    async def _read(session):
        row = (await session.execute(select(SystemConfig).where(SystemConfig.key == SETTINGS_KEY))).scalar_one_or_none()
        try:
            return json.loads(row.value) if row and row.value else {}
        except ValueError:
            return {}
    if db is not None:
        raw = await _read(db)
    else:
        async with AsyncSessionLocal() as session:
            raw = await _read(session)
    _settings = _normalize_settings(raw)
    return _settings


async def save_settings(db, update: dict) -> dict:
    global _settings
    merged = _normalize_settings({**(await load_settings(db)), **update})
    stored = {**merged, "known_events": list(EVENTS)}
    row = (await db.execute(select(SystemConfig).where(SystemConfig.key == SETTINGS_KEY))).scalar_one_or_none()
    if row is None:
        db.add(SystemConfig(key=SETTINGS_KEY, value=json.dumps(stored)))
    else:
        row.value = json.dumps(stored)
    _settings = merged
    return merged


async def settings() -> dict:
    return _settings if _settings is not None else await load_settings()
