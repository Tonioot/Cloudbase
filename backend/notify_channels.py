"""Notification channel types: what each one needs and how it sends.

Every sender gets a Message and the channel's decrypted config, and raises
on failure (the dispatcher retries). Only httpx and smtplib — no SDKs.
"""

import asyncio
import hashlib
import hmac
import html
import json
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Optional

import httpx

_TIMEOUT = httpx.Timeout(15.0, connect=10.0)
_USER_AGENT = "Cloudbase-Notifications"

# Colours per severity for Slack, Discord and Teams
_COLOURS = {"critical": "#dc2626", "warning": "#d97706", "info": "#2563eb", "resolved": "#16a34a"}
_EMOJI = {"critical": "🔴", "warning": "🟠", "info": "🔵", "resolved": "🟢"}


@dataclass
class Message:
    id: int
    event: str
    severity: str          # info | warning | critical | resolved
    title: str
    message: str
    url: Optional[str]     # absolute link into the panel, when the panel has a domain
    app: Optional[str]
    dedupe_key: str        # stable per problem; a resolve carries the key of what it resolves
    resolve: bool
    timestamp: str         # ISO 8601, UTC
    detail: dict = field(default_factory=dict)

    def payload(self) -> dict:
        return {
            "id": self.id, "event": self.event, "severity": self.severity,
            "status": "resolved" if self.resolve else "triggered",
            "title": self.title, "message": self.message, "app": self.app,
            "url": self.url, "dedupe_key": self.dedupe_key, "timestamp": self.timestamp,
        }


# Field: (key, label, kind, required, help). kind: text | secret | number | select:<a>|<b>
CHANNEL_TYPES: dict[str, dict] = {
    "slack": {
        "label": "Slack",
        "fields": [("webhook_url", "Incoming webhook URL", "secret", True,
                    "Slack → Apps → Incoming Webhooks → Add New Webhook to Workspace")],
    },
    "teams": {
        "label": "Microsoft Teams",
        "fields": [("webhook_url", "Workflow webhook URL", "secret", True,
                    "In the channel: Workflows → \"Post to a channel when a webhook request is received\"")],
    },
    "pagerduty": {
        "label": "PagerDuty",
        "fields": [("routing_key", "Integration key", "secret", True,
                    "Service → Integrations → Events API v2. Problems open an incident and resolve it again by themselves.")],
    },
    "email": {
        "label": "E-mail",
        "fields": [
            ("to", "Recipients", "text", True, "Comma-separated addresses"),
            ("smtp_host", "SMTP server", "text", True, "e.g. smtp.gmail.com or smtp.eu.mailgun.org"),
            ("smtp_port", "Port", "number", True, "587 for STARTTLS, 465 for TLS"),
            ("smtp_security", "Security", "select:starttls|tls|none", True, ""),
            ("smtp_username", "Username", "text", False, ""),
            ("smtp_password", "Password", "secret", False, ""),
            ("from_address", "From", "text", True, "e.g. Cloudbase <alerts@example.com>"),
        ],
    },
    "webhook": {
        "label": "Webhook",
        "fields": [
            ("url", "URL", "secret", True, "Receives a JSON POST for every notification"),
            ("secret", "Signing secret", "secret", False,
             "Optional. Signs the body: X-Cloudbase-Signature: sha256=<HMAC-SHA256 hex>"),
        ],
    },
    "discord": {
        "label": "Discord",
        "fields": [("webhook_url", "Webhook URL", "secret", True,
                    "Channel settings → Integrations → Webhooks → New Webhook → Copy URL")],
    },
    "telegram": {
        "label": "Telegram",
        "fields": [
            ("bot_token", "Bot token", "secret", True, "From @BotFather"),
            ("chat_id", "Chat ID", "text", True, "A user, group or channel ID (groups start with -)"),
        ],
    },
    "ntfy": {
        "label": "ntfy",
        "fields": [
            ("server", "Server", "text", True, "https://ntfy.sh or your own server"),
            ("topic", "Topic", "text", True, ""),
            ("token", "Access token", "secret", False, "Only for protected topics"),
        ],
    },
}


def secret_fields(channel_type: str) -> set[str]:
    return {f[0] for f in CHANNEL_TYPES[channel_type]["fields"] if f[2] == "secret"}


def validate(channel_type: str, config: dict) -> Optional[str]:
    """An error message for missing or malformed fields, or None."""
    spec = CHANNEL_TYPES.get(channel_type)
    if spec is None:
        return f"Unknown channel type '{channel_type}'"
    for key, label, kind, required, _help in spec["fields"]:
        value = str(config.get(key) or "").strip()
        if required and not value:
            return f"{label} is required"
        if value and kind == "number" and not value.isdigit():
            return f"{label} must be a number"
        if value and kind.startswith("select:") and value not in kind[7:].split("|"):
            return f"{label} must be one of {kind[7:].replace('|', ', ')}"
    for key in ("webhook_url", "url", "server"):
        value = str(config.get(key) or "").strip()
        if value and not value.startswith(("https://", "http://")):
            return "URLs must start with https://"
    return None


def _text(msg: Message) -> str:
    """Title + message + link, as plain text."""
    parts = [msg.title]
    if msg.message:
        parts.append(msg.message)
    if msg.url:
        parts.append(msg.url)
    return "\n".join(parts)


async def _post(url: str, *, json_body=None, content: Optional[bytes] = None, headers: Optional[dict] = None) -> None:
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
        r = await client.post(url, json=json_body, content=content,
                              headers={"User-Agent": _USER_AGENT, **(headers or {})})
    if r.status_code >= 300:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")


# ── Senders ───────────────────────────────────────────────────────────────────

async def _send_slack(msg: Message, cfg: dict) -> None:
    text = f"{_EMOJI.get(msg.severity, '')} *{msg.title}*"
    if msg.message:
        text += f"\n{msg.message}"
    if msg.url:
        text += f"\n<{msg.url}|Open in Cloudbase>"
    await _post(cfg["webhook_url"], json_body={
        "text": f"{_EMOJI.get(msg.severity, '')} {msg.title}",  # notification preview
        "attachments": [{"color": _COLOURS.get(msg.severity), "mrkdwn_in": ["text"], "text": text}],
    })


async def _send_teams(msg: Message, cfg: dict) -> None:
    body = [{"type": "TextBlock", "text": f"{_EMOJI.get(msg.severity, '')} {msg.title}",
             "weight": "Bolder", "size": "Medium", "wrap": True}]
    if msg.message:
        body.append({"type": "TextBlock", "text": msg.message, "wrap": True})
    card = {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard", "version": "1.4", "body": body}
    if msg.url:
        card["actions"] = [{"type": "Action.OpenUrl", "title": "Open in Cloudbase", "url": msg.url}]
    await _post(cfg["webhook_url"], json_body={"type": "message", "attachments": [
        {"contentType": "application/vnd.microsoft.card.adaptive", "content": card},
    ]})


async def _send_pagerduty(msg: Message, cfg: dict) -> None:
    event = {"routing_key": cfg["routing_key"], "dedup_key": msg.dedupe_key,
             "event_action": "resolve" if msg.resolve else "trigger"}
    if not msg.resolve:
        event["payload"] = {
            "summary": msg.title[:1024], "source": msg.app or "cloudbase",
            "severity": {"critical": "critical", "warning": "warning"}.get(msg.severity, "info"),
            "component": msg.app, "class": msg.event, "timestamp": msg.timestamp,
            "custom_details": {"message": msg.message, **msg.detail},
        }
        if msg.url:
            event["links"] = [{"href": msg.url, "text": "Open in Cloudbase"}]
    await _post("https://events.pagerduty.com/v2/enqueue", json_body=event)


def _smtp_send(cfg: dict, email: EmailMessage) -> None:
    host, port = cfg["smtp_host"].strip(), int(cfg.get("smtp_port") or 587)
    security = cfg.get("smtp_security") or "starttls"
    context = ssl.create_default_context()
    if security == "tls":
        server = smtplib.SMTP_SSL(host, port, timeout=20, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=20)
    with server:
        if security == "starttls":
            server.starttls(context=context)
        if cfg.get("smtp_username"):
            server.login(cfg["smtp_username"], cfg.get("smtp_password") or "")
        server.send_message(email)


async def _send_email(msg: Message, cfg: dict) -> None:
    email = EmailMessage()
    email["Subject"] = f"[Cloudbase] {msg.title}"
    email["From"] = cfg["from_address"].strip()
    email["To"] = ", ".join(a.strip() for a in cfg["to"].split(",") if a.strip())
    email.set_content(_text(msg) + "\n\n— Cloudbase")
    link = (f'<p><a href="{html.escape(msg.url)}" style="color:#2563eb">Open in Cloudbase</a></p>'
            if msg.url else "")
    email.add_alternative(
        f'<div style="font-family:system-ui,sans-serif;max-width:560px">'
        f'<div style="border-left:4px solid {_COLOURS.get(msg.severity, "#64748b")};padding:4px 12px">'
        f'<h2 style="margin:0 0 8px;font-size:17px">{html.escape(msg.title)}</h2>'
        f'<p style="margin:0;white-space:pre-wrap;color:#334155">{html.escape(msg.message or "")}</p>'
        f'{link}</div><p style="color:#94a3b8;font-size:12px">Sent by Cloudbase</p></div>',
        subtype="html",
    )
    await asyncio.to_thread(_smtp_send, cfg, email)


async def _send_webhook(msg: Message, cfg: dict) -> None:
    body = json.dumps(msg.payload(), separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json", "X-Cloudbase-Event": msg.event}
    if cfg.get("secret"):
        digest = hmac.new(cfg["secret"].encode(), body, hashlib.sha256).hexdigest()
        headers["X-Cloudbase-Signature"] = f"sha256={digest}"
    await _post(cfg["url"], content=body, headers=headers)


async def _send_discord(msg: Message, cfg: dict) -> None:
    embed = {"title": f"{_EMOJI.get(msg.severity, '')} {msg.title}"[:256],
             "description": (msg.message or "")[:4000],
             "color": int(_COLOURS.get(msg.severity, "#64748b")[1:], 16),
             "timestamp": msg.timestamp}
    if msg.url:
        embed["url"] = msg.url
    await _post(cfg["webhook_url"], json_body={"username": "Cloudbase", "embeds": [embed],
                                               "allowed_mentions": {"parse": []}})


async def _send_telegram(msg: Message, cfg: dict) -> None:
    text = f"{_EMOJI.get(msg.severity, '')} <b>{html.escape(msg.title)}</b>"
    if msg.message:
        text += f"\n{html.escape(msg.message)}"
    if msg.url:
        text += f'\n<a href="{html.escape(msg.url)}">Open in Cloudbase</a>'
    await _post(f"https://api.telegram.org/bot{cfg['bot_token'].strip()}/sendMessage", json_body={
        "chat_id": cfg["chat_id"].strip(), "text": text[:4096],
        "parse_mode": "HTML", "disable_web_page_preview": True,
    })


async def _send_ntfy(msg: Message, cfg: dict) -> None:
    body = {"topic": cfg["topic"].strip(), "title": msg.title, "message": msg.message or msg.title,
            "priority": {"critical": 5, "warning": 4, "resolved": 3}.get(msg.severity, 3),
            "tags": [{"critical": "rotating_light", "warning": "warning",
                      "resolved": "white_check_mark"}.get(msg.severity, "information_source")]}
    if msg.url:
        body["click"] = msg.url
    headers = {"Authorization": f"Bearer {cfg['token']}"} if cfg.get("token") else None
    await _post(cfg["server"].strip().rstrip("/"), json_body=body, headers=headers)


_SENDERS = {
    "slack": _send_slack, "teams": _send_teams, "pagerduty": _send_pagerduty, "email": _send_email,
    "webhook": _send_webhook, "discord": _send_discord, "telegram": _send_telegram, "ntfy": _send_ntfy,
}


async def send(channel_type: str, cfg: dict, msg: Message) -> None:
    await asyncio.wait_for(_SENDERS[channel_type](msg, cfg), timeout=45)
