"""The notification e-mail: Cloudbase styling, no tracking, delivered as normal mail.

- Black and white like the panel, light by default and black in dark mode.
- The logo is embedded (cid:), so no remote image is loaded — those are how
  opens are tracked, and they get blocked or flagged.
- Tracking is switched off for the SMTP services that add it by default, and
  links point straight at the panel (no redirects).
- It's marked as an automatic system message (Auto-Submitted), without the
  bulk / unsubscribe headers of newsletters. Whether it lands in the inbox
  mostly depends on SPF, DKIM and DMARC for the From domain.
"""

import hashlib
import html
import json
import os
import socket
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from typing import Optional

_LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "cloudbase-mark.png")
_LOGO_CID = "cloudbase-logo@cloudbase"

_SEVERITY = {"critical": "Critical", "warning": "Warning", "info": "Info", "resolved": "Resolved"}
_DOT = {"critical": "#dc2626", "warning": "#d97706", "info": "#71717a", "resolved": "#16a34a"}
_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"

# Ask the SMTP services that track by default not to: no open pixel, no rewritten links.
_NO_TRACKING_HEADERS = {
    "X-Mailjet-TrackOpen": "0",
    "X-Mailjet-TrackClick": "0",
    "X-Mailgun-Track": "no",
    "X-Mailgun-Track-Clicks": "no",
    "X-Mailgun-Track-Opens": "no",
    "X-PM-TrackOpens": "false",
    "X-PM-TrackLinks": "None",
    "X-SMTPAPI": json.dumps({"filters": {"clicktrack": {"settings": {"enable": 0}},
                                         "opentrack": {"settings": {"enable": 0}}}}),
    "X-MSYS-API": json.dumps({"options": {"open_tracking": False, "click_tracking": False}}),
}

# Same installation → same Message-IDs, so an all-clear threads under its alert.
_INSTALL = hashlib.sha256(f"{socket.getfqdn()}:{os.path.expanduser('~')}".encode()).hexdigest()[:10]


def _message_id(notification_id: int, domain: str) -> str:
    return f"<cloudbase.{notification_id}.{_INSTALL}@{domain}>"


def _when(timestamp: str) -> str:
    try:
        return datetime.fromisoformat(timestamp).astimezone().strftime("%d %b %Y, %H:%M %Z").lstrip("0")
    except ValueError:
        return timestamp


def _host(url: Optional[str]) -> Optional[str]:
    return url.split("/")[2] if url and "://" in url else None


def _text(msg, when: str) -> str:
    lines = [f"{_SEVERITY.get(msg.severity, '')} · {msg.event_label}", "", msg.title]
    if msg.message:
        lines += ["", msg.message]
    lines.append("")
    if msg.app:
        lines.append(f"App:  {msg.app}")
    lines.append(f"Time: {when}")
    if msg.url:
        lines += ["", f"Open in Cloudbase: {msg.url}"]
    lines += ["", "—", "Sent by Cloudbase" + (f" ({_host(msg.settings_url)})" if msg.settings_url else "")]
    if msg.settings_url:
        lines.append(f"Notification settings: {msg.settings_url}")
    return "\n".join(lines) + "\n"


def _html(msg, when: str, has_logo: bool) -> str:
    e = html.escape
    sev = msg.severity
    body = e(msg.message or "").replace("\n", "<br>")
    preheader = e((msg.message or msg.title).split("\n")[0][:140])
    logo = (f'<img src="cid:{_LOGO_CID}" width="28" height="28" alt="" '
            f'style="display:block;width:28px;height:28px;border:0;border-radius:7px">') if has_logo else ""
    rows = ([("App", e(msg.app))] if msg.app else []) + [("Time", e(when))]
    details = "".join(
        f'<tr><td class="cb-muted" style="vertical-align:top;padding:4px 16px 4px 0;font:13px/1.5 {_FONT};color:#71717a;white-space:nowrap">{k}</td>'
        f'<td class="cb-text" style="vertical-align:top;padding:4px 0;font:13px/1.5 {_FONT};color:#18181b">{v}</td></tr>'
        for k, v in rows
    )
    button = (
        f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin-top:24px"><tr>'
        f'<td class="cb-btn" style="background:#0a0a0a;border-radius:8px">'
        f'<a href="{e(msg.url)}" style="display:inline-block;padding:10px 18px;font:500 13px/1 {_FONT};'
        f'color:#ffffff;text-decoration:none;border-radius:8px">Open in Cloudbase</a></td></tr></table>'
    ) if msg.url else ""
    host = _host(msg.settings_url)
    footer = "Sent by Cloudbase" + (f" on {e(host)}" if host else "") + "."
    if msg.settings_url:
        footer += (f' <a href="{e(msg.settings_url)}" class="cb-muted" '
                   f'style="color:#71717a;text-decoration:underline">Notification settings</a>')

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="supported-color-schemes" content="light dark">
<title>{e(msg.title)}</title>
<style>
  @media (prefers-color-scheme: dark) {{
    .cb-bg {{ background:#000000 !important; }}
    .cb-card {{ background:#0a0a0a !important; border-color:#27272a !important; }}
    .cb-text {{ color:#fafafa !important; }}
    .cb-muted {{ color:#a1a1aa !important; }}
    .cb-line {{ border-color:#27272a !important; }}
    .cb-pill {{ border-color:#3f3f46 !important; color:#d4d4d8 !important; }}
    .cb-btn {{ background:#fafafa !important; }}
    .cb-btn a {{ color:#0a0a0a !important; }}
  }}
</style>
</head>
<body class="cb-bg" style="margin:0;padding:0;background:#f4f4f5;-webkit-text-size-adjust:100%">
<div style="display:none;max-height:0;overflow:hidden;opacity:0">{preheader}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" class="cb-bg" style="background:#f4f4f5">
<tr><td align="center" style="padding:32px 16px">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px">
    <tr><td style="padding:0 4px 16px">
      <table role="presentation" cellpadding="0" cellspacing="0"><tr>
        <td style="padding-right:{10 if has_logo else 0}px">{logo}</td>
        <td class="cb-text" style="font:600 15px/1 {_FONT};color:#0a0a0a;letter-spacing:-0.01em">Cloudbase</td>
      </tr></table>
    </td></tr>
    <tr><td class="cb-card" style="background:#ffffff;border:1px solid #e4e4e7;border-radius:12px;padding:28px">
      <table role="presentation" cellpadding="0" cellspacing="0"><tr>
        <td class="cb-pill" style="border:1px solid #e4e4e7;border-radius:999px;padding:3px 10px;font:12px/1.4 {_FONT};color:#3f3f46">
          <span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:{_DOT.get(sev, '#71717a')};margin-right:6px;vertical-align:1px"></span>{e(_SEVERITY.get(sev, ''))} · {e(msg.event_label)}
        </td>
      </tr></table>
      <h1 class="cb-text" style="margin:16px 0 0;font:600 19px/1.35 {_FONT};color:#0a0a0a;letter-spacing:-0.01em">{e(msg.title)}</h1>
      {f'<p class="cb-text" style="margin:10px 0 0;font:14px/1.6 {_FONT};color:#3f3f46;word-break:break-word">{body}</p>' if body else ''}
      <table role="presentation" cellpadding="0" cellspacing="0" width="100%" class="cb-line" style="margin-top:20px;border-top:1px solid #e4e4e7">
        <tr><td style="padding-top:12px"><table role="presentation" cellpadding="0" cellspacing="0">{details}</table></td></tr>
      </table>
      {button}
    </td></tr>
    <tr><td class="cb-muted" style="padding:16px 4px 0;font:12px/1.6 {_FONT};color:#71717a">{footer}</td></tr>
  </table>
</td></tr>
</table>
</body>
</html>"""


def build(msg, cfg: dict) -> EmailMessage:
    when = _when(msg.timestamp)
    sender = cfg["from_address"].strip()
    domain = parseaddr(sender)[1].rpartition("@")[2] or "cloudbase.local"

    email = EmailMessage()
    email["Subject"] = msg.title
    email["From"] = sender
    email["To"] = ", ".join(a.strip() for a in cfg["to"].split(",") if a.strip())
    email["Date"] = formatdate(localtime=True)
    email["Message-ID"] = _message_id(msg.id, domain) if msg.id else make_msgid("cloudbase.test", domain)
    if msg.resolves_id:
        email["In-Reply-To"] = email["References"] = _message_id(msg.resolves_id, domain)
    # An automatic system message (RFC 3834): no auto-replies, not a newsletter
    email["Auto-Submitted"] = "auto-generated"
    email["X-Auto-Response-Suppress"] = "All"
    for k, v in _NO_TRACKING_HEADERS.items():
        email[k] = v

    try:
        with open(_LOGO_PATH, "rb") as f:
            logo = f.read()
    except OSError:
        logo = None
    email.set_content(_text(msg, when))
    email.add_alternative(_html(msg, when, bool(logo)), subtype="html")
    if logo:
        email.get_payload()[1].add_related(logo, maintype="image", subtype="png",
                                           cid=f"<{_LOGO_CID}>", disposition="inline", filename="cloudbase.png")
    return email
