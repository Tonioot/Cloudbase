import json
from typing import Optional
from notify import channels
from core.env_crypto import encrypt_text, decrypt_text
from core.models import NotificationChannel

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
