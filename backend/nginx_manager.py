import logging
import os
import subprocess
import hashlib

log = logging.getLogger("pdm.nginx")

NGINX_SITES_DIR = "/etc/nginx/sites-available"
NGINX_ENABLED_DIR = "/etc/nginx/sites-enabled"
MAINTENANCE_DIR = "/var/www/cloudbase/maintenance"


def _normalize_domain(value: str) -> str:
  """Convert user input to a plain hostname for nginx server_name.

  Accepts values like https://example.com/path and returns example.com.
  """
  raw = (value or "").strip()
  if not raw:
    return ""

  # Remove common accidental wrappers from UI/input copy-paste.
  raw = raw.strip('"\'`').strip()

  if "://" in raw:
    from urllib.parse import urlsplit
    split = urlsplit(raw)
    raw = split.netloc or split.path

  raw = raw.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].strip()
  raw = raw.strip('"\'`').strip()
  if ":" in raw and raw.count(":") == 1:
    # Drop a single trailing port suffix host:443
    raw = raw.split(":", 1)[0]

  # Keep only nginx server_name-safe hostname characters.
  import re as _re
  raw = _re.sub(r"[^a-zA-Z0-9.*-]", "", raw)
  raw = raw.strip(".")
  return raw.lower()


def _sanitize_ssl_path(value: str | None) -> str | None:
  """Return a nginx-safe absolute cert/key path or None.

  Rejects values containing quotes, semicolons or newlines to avoid
  breaking nginx directives.
  """
  if not value:
    return None

  raw = str(value).strip().strip('"\'`').strip()
  if not raw:
    return None
  if any(ch in raw for ch in ('"', "'", "`", ";", "\n", "\r")):
    return None
  if not raw.startswith("/"):
    return None
  return raw


# â"€â"€ Maintenance page HTML generation â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

def generate_maintenance_html(
    title: str,
    message: str,
    color: str,
    status_url: str = None,
    custom_html: str = None,
    page_type: str = "downtime",
    logo_data: str = None,
    dark_mode: str = "auto",
) -> str:
    """Return a full HTML page for downtime or update mode. Uses custom_html if provided."""
    if custom_html:
        return custom_html

    safe_title   = (title or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    safe_message = (message or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    safe_color   = color if color and color.startswith("#") and len(color) in (4, 7) else "#f85149"
    safe_dark    = dark_mode if dark_mode in ("auto", "light", "dark") else "auto"
    # Validate URL to prevent injection
    import re as _re
    safe_status_url = status_url if status_url and _re.match(r'^https?://', status_url) else None
    # Validate logo: must be a data-URL with an image MIME type
    safe_logo_data = logo_data if logo_data and _re.match(r'^data:image/[a-zA-Z0-9+/.-]+;base64,', logo_data) else None

    if page_type == "downtime":
        return _downtime_template(safe_title, safe_message, safe_color, safe_status_url, safe_logo_data, safe_dark)
    if page_type == "restart":
        return _restart_template(safe_title, safe_message, safe_color, safe_status_url, safe_logo_data, safe_dark)
    if page_type == "starting":
        return _starting_template(safe_title, safe_message, safe_color, safe_status_url, safe_logo_data, safe_dark)
    return _update_template(safe_title, safe_message, safe_color, safe_status_url, safe_logo_data, safe_dark)


def generate_cloudbase_unavailable_html(domain: str | None = None) -> str:
    """Return a branded Cloudbase unavailable page as a single clean card."""
    safe_domain = (domain or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    domain_line = f"Primary domain: {safe_domain}" if safe_domain else "Cloudbase endpoint is temporarily unavailable"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="refresh" content="8">
  <title>Cloudbase Unavailable</title>
  <style>
    :root {{
      --bg-base: #0a0a0a;
      --bg-surface: #111111;
      --bg-elevated: #1a1a1a;
      --bg-muted: #222222;
      --border: #2e2e2e;
      --text-primary: #f0f0f0;
      --text-secondary: #a0a0a0;
      --text-muted: #606060;
      --accent: #c8c8c8;
      --accent-bg: rgba(200, 200, 200, 0.08);
      --accent-border: rgba(200, 200, 200, 0.18);
      --red: #f87171;
      --red-bg: rgba(248, 113, 113, 0.1);
      --red-border: rgba(248, 113, 113, 0.25);
    }}

    * {{ box-sizing: border-box; margin: 0; padding: 0; }}

    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Inter', sans-serif;
      background:
        radial-gradient(120% 80% at 0% 0%, rgba(200,200,200,0.08), transparent 52%),
        radial-gradient(120% 80% at 100% 100%, rgba(200,200,200,0.05), transparent 58%),
        var(--bg-base);
      color: var(--text-primary);
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 24px;
    }}

    .card {{
      width: min(460px, 100%);
      background: linear-gradient(180deg, rgba(20,20,20,0.96), rgba(12,12,12,0.94));
      border: 1px solid var(--border);
      border-radius: 18px;
      box-shadow: 0 24px 60px rgba(0,0,0,0.7);
      padding: 30px 28px 24px;
    }}

    .brand-row {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 18px;
    }}

    .brand {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-size: 15px;
      font-weight: 700;
      letter-spacing: .01em;
      color: var(--text-primary);
    }}

    .brand-dot {{
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--red);
      box-shadow: 0 0 0 3px var(--red-bg);
    }}

    .badge {{
      padding: 4px 10px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 600;
      color: var(--red);
      background: var(--red-bg);
      border: 1px solid var(--red-border);
      white-space: nowrap;
    }}

    h1 {{
      font-size: 22px;
      line-height: 1.2;
      margin-bottom: 9px;
      letter-spacing: -0.02em;
    }}

    p {{
      color: var(--text-secondary);
      line-height: 1.6;
      font-size: 14px;
    }}

    .meta-line {{
      margin-top: 14px;
      padding: 9px 10px;
      border-radius: 10px;
      border: 1px solid var(--accent-border);
      background: var(--accent-bg);
      color: var(--text-secondary);
      font-size: 12px;
      line-height: 1.45;
    }}

    .actions {{
      margin-top: 16px;
      display: flex;
      gap: 8px;
    }}

    .btn {{
      border: 1px solid var(--border);
      background: var(--bg-elevated);
      color: var(--text-primary);
      text-decoration: none;
      padding: 8px 12px;
      border-radius: 10px;
      font-size: 13px;
      transition: 160ms ease;
    }}

    .btn:hover {{
      background: var(--bg-muted);
      border-color: #444;
    }}

    @media (max-width: 540px) {{
      .card {{
        padding: 24px 18px 18px;
      }}

      h1 {{
        font-size: 20px;
      }}
    }}
  </style>
</head>
<body>
  <section class="card">
    <div class="brand-row">
      <div class="brand"><span class="brand-dot"></span><span>Cloudbase</span></div>
    </div>
    <h1>Cloudbase is restarting or temporarily offline</h1>
    <p>The dashboard is currently unavailable while services are recovering. This page refreshes automatically every few seconds.</p>
    <div class="meta-line">{domain_line}</div>
  </section>
</body>
</html>
"""


def generate_cloudbase_unknown_host_html(domain: str | None = None) -> str:
    """Return a branded page for unknown/unconfigured hostnames."""
    safe_domain = (domain or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    hint = (
        f"Use the configured Cloudbase panel domain: {safe_domain}"
        if safe_domain else
        "This hostname is not configured in Cloudbase"
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>App Not Found</title>
  <style>
    :root {{
      --bg-base: #0a0a0a;
      --bg-surface: #111111;
      --border: #2e2e2e;
      --text-primary: #f0f0f0;
      --text-secondary: #a0a0a0;
      --accent: #c8c8c8;
      --accent-bg: rgba(200, 200, 200, 0.08);
      --accent-border: rgba(200, 200, 200, 0.2);
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Inter', sans-serif;
      background: radial-gradient(120% 80% at 0% 0%, rgba(200,200,200,0.08), transparent 52%), var(--bg-base);
      color: var(--text-primary);
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 24px;
    }}
    .card {{
      width: min(500px, 100%);
      background: linear-gradient(180deg, rgba(20,20,20,0.96), rgba(12,12,12,0.94));
      border: 1px solid var(--border);
      border-radius: 18px;
      box-shadow: 0 24px 60px rgba(0,0,0,0.7);
      padding: 30px 28px 24px;
    }}
    .brand {{ display: inline-flex; align-items: center; gap: 8px; font-size: 15px; font-weight: 700; margin-bottom: 16px; }}
    .dot {{ width: 8px; height: 8px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 0 3px var(--accent-bg); }}
    h1 {{ font-size: 22px; line-height: 1.25; margin-bottom: 9px; }}
    p {{ color: var(--text-secondary); line-height: 1.6; font-size: 14px; }}
    .hint {{
      margin-top: 14px;
      padding: 9px 10px;
      border-radius: 10px;
      border: 1px solid var(--accent-border);
      background: var(--accent-bg);
      color: var(--text-secondary);
      font-size: 12px;
      line-height: 1.45;
    }}
  </style>
</head>
<body>
  <section class="card">
    <div class="brand"><span class="dot"></span><span>Cloudbase</span></div>
    <h1>App does not exist on this domain</h1>
    <p>The requested hostname is not linked to an app in Cloudbase.</p>
  </section>
</body>
</html>
"""


def _render_visual_block(color: str, icon_svg: str, logo_data: str = None) -> str:
    inner = (
        f'<span class="icon-logo"><img src="{logo_data}" alt="Logo" /></span>'
        if logo_data else
        f'<span class="icon-glyph">{icon_svg}</span>'
    )
    return f'<div class="icon-ring">{inner}</div>'


def _base_css(color: str, spin_dur: str = "2.5s", dark_mode: str = "auto") -> str:
    dark_vars = """
        --bg:     #0d0d0f;
        --card:   #141418;
        --border: #27272f;
        --shadow: rgba(0,0,0,.5);
        --h1:     #f4f4f5;
        --msg:    #8c8c9a;
        --foot:   #4a4a5a;
        --track:  #27272f;"""
    if dark_mode == "dark":
        dark_section = f":root {{{dark_vars}\n    }}"
    elif dark_mode == "light":
        dark_section = ""
    else:
        dark_section = f"@media (prefers-color-scheme: dark) {{\n      :root {{{dark_vars}\n      }}\n    }}"

    return f"""
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{
      --bg:        #f1f5f9;
      --card:      #ffffff;
      --border:    #e2e8f0;
      --shadow:    rgba(0,0,0,.07);
      --h1:        #0f172a;
      --msg:       #64748b;
      --foot:      #94a3b8;
      --track:     #e2e8f0;
      --accent:    {color};
    }}
    {dark_section}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Inter', sans-serif;
      background: var(--bg);
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 40px 20px;
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 24px;
      box-shadow: 0 1px 3px rgba(0,0,0,.04), 0 12px 40px var(--shadow);
      padding: 56px 48px 48px;
      max-width: 480px;
      width: 100%;
      text-align: center;
    }}
    .icon-ring {{
      width: 80px; height: 80px;
      border-radius: 50%;
      background: color-mix(in srgb, var(--accent) 10%, var(--card));
      border: 1.5px solid color-mix(in srgb, var(--accent) 22%, transparent);
      display: flex; align-items: center; justify-content: center;
      margin: 0 auto 28px;
      position: relative;
    }}
    .icon-glyph {{ display: inline-flex; align-items: center; justify-content: center; }}
    .icon-glyph svg {{ display: block; }}
    .icon-logo {{
      width: 56px; height: 56px;
      border-radius: 50%;
      overflow: hidden;
      display: flex; align-items: center; justify-content: center;
      background: var(--card);
    }}
    .icon-logo img {{
      width: 100%; height: 100%;
      object-fit: contain;
      padding: 8px;
    }}
    .icon-ring::before {{
      content: '';
      position: absolute; inset: -6px; border-radius: 50%;
      border: 2px solid color-mix(in srgb, var(--accent) 14%, transparent);
      border-top-color: var(--accent);
      animation: spin {spin_dur} linear infinite;
    }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    .badge {{
      display: inline-flex; align-items: center; gap: 7px;
      background: color-mix(in srgb, var(--accent) 10%, var(--card));
      border: 1px solid color-mix(in srgb, var(--accent) 24%, transparent);
      border-radius: 100px; padding: 5px 15px; margin-bottom: 24px;
      font-size: 10.5px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase;
      color: var(--accent);
    }}
    .dot {{ width: 6px; height: 6px; border-radius: 50%; background: var(--accent); animation: blink 1.8s ease-in-out infinite; }}
    @keyframes blink {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: .25; }} }}
    .spinner {{
      width: 8px; height: 8px; border-radius: 50%;
      border: 2px solid color-mix(in srgb, var(--accent) 22%, transparent);
      border-top-color: var(--accent);
      animation: spin .7s linear infinite;
    }}
    h1 {{ font-size: 27px; font-weight: 700; color: var(--h1); letter-spacing: -.03em; line-height: 1.25; margin-bottom: 14px; }}
    .msg {{ font-size: 15px; color: var(--msg); line-height: 1.85; margin-bottom: 30px; }}
    .divider {{ width: 40px; height: 2px; background: linear-gradient(90deg, transparent, var(--accent), transparent); margin: 0 auto 26px; border-radius: 2px; }}
    .track {{ background: var(--track); border-radius: 100px; height: 3px; overflow: hidden; margin-bottom: 30px; }}
    .bar {{ height: 100%; background: linear-gradient(90deg, transparent, var(--accent), transparent); animation: sweep 1.4s ease-in-out infinite; }}
    @keyframes sweep {{ 0% {{ transform: translateX(-100%) scaleX(.6); }} 100% {{ transform: translateX(200%) scaleX(.6); }} }}
    .status-link {{
      display: inline-flex; align-items: center; gap: 7px;
      font-size: 13px; font-weight: 500; color: var(--accent);
      text-decoration: none; padding: 10px 22px;
      border: 1px solid color-mix(in srgb, var(--accent) 30%, transparent);
      border-radius: 12px;
      background: color-mix(in srgb, var(--accent) 7%, transparent);
      transition: background .15s, border-color .15s;
    }}
    .status-link:hover {{
      background: color-mix(in srgb, var(--accent) 14%, transparent);
      border-color: color-mix(in srgb, var(--accent) 45%, transparent);
    }}
    .footer {{ margin-top: 36px; font-size: 11px; color: var(--foot); line-height: 1.6; }}
    @media (max-width: 500px) {{
      .card {{ padding: 40px 28px 36px; border-radius: 18px; }}
      h1 {{ font-size: 23px; }}
    }}"""


def _downtime_template(title: str, message: str, color: str, status_url: str = None, logo_data: str = None, dark_mode: str = "auto") -> str:
    status_btn = f"""
    <a class="status-link" href="{status_url}" target="_blank" rel="noopener noreferrer">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="22 12 16 12 13 21 11 3 8 12 2 12"/></svg>
      View status page
      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="7" y1="17" x2="17" y2="7"/><polyline points="7 7 17 7 17 17"/></svg>
    </a>""" if status_url else ""
    visual_block = _render_visual_block(
        color,
        f"""<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="1.5" stroke-linecap="round">
        <circle cx="12" cy="12" r="10"/>
        <polyline points="12 6 12 12 16 14"/>
      </svg>""",
        logo_data,
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <style>{_base_css(color, "2.5s", dark_mode)}</style>
</head>
<body>
  <div class="card">
    {visual_block}
    <div class="badge"><span class="dot"></span> Down for maintenance</div>
    <h1>{title}</h1>
    <p class="msg">{message}</p>
    <div class="divider"></div>
    {status_btn}
    <div class="footer">Powered by Cloudbase &middot; We&rsquo;re working on it &mdash; this page updates automatically.</div>
  </div>
</body>
</html>
"""


def _restart_template(title: str, message: str, color: str, status_url: str = None, logo_data: str = None, dark_mode: str = "auto") -> str:
    status_btn = f"""
    <a class="status-link" href="{status_url}" target="_blank" rel="noopener noreferrer">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="22 12 16 12 13 21 11 3 8 12 2 12"/></svg>
      View status page
      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="7" y1="17" x2="17" y2="7"/><polyline points="7 7 17 7 17 17"/></svg>
    </a>""" if status_url else ""
    visual_block = _render_visual_block(
        color,
        f"""<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="1.5" stroke-linecap="round">
        <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/>
        <path d="M3 3v5h5"/>
      </svg>""",
        logo_data,
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="refresh" content="8">
  <title>{title}</title>
  <style>{_base_css(color, ".9s", dark_mode)}</style>
</head>
<body>
  <div class="card">
    {visual_block}
    <div class="badge"><span class="spinner"></span> Restarting</div>
    <h1>{title}</h1>
    <p class="msg">{message}</p>
    <div class="track"><div class="bar"></div></div>
    {status_btn}
    <div class="footer">Powered by Cloudbase &middot; Page auto-refreshes every 8 seconds.</div>
  </div>
</body>
</html>
"""


def _starting_template(title: str, message: str, color: str, status_url: str = None, logo_data: str = None, dark_mode: str = "auto") -> str:
    status_btn = f"""
    <a class="status-link" href="{status_url}" target="_blank" rel="noopener noreferrer">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="22 12 16 12 13 21 11 3 8 12 2 12"/></svg>
      View status page
      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="7" y1="17" x2="17" y2="7"/><polyline points="7 7 17 7 17 17"/></svg>
    </a>""" if status_url else ""
    visual_block = _render_visual_block(
        color,
        f"""<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
        <polygon points="5 3 19 12 5 21 5 3"/>
      </svg>""",
        logo_data,
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="refresh" content="8">
  <title>{title}</title>
  <style>{_base_css(color, ".9s", dark_mode)}</style>
</head>
<body>
  <div class="card">
    {visual_block}
    <div class="badge"><span class="spinner"></span> Starting up</div>
    <h1>{title}</h1>
    <p class="msg">{message}</p>
    <div class="track"><div class="bar"></div></div>
    {status_btn}
    <div class="footer">Powered by Cloudbase &middot; Page auto-refreshes every 8 seconds.</div>
  </div>
</body>
</html>
"""


def _update_template(title: str, message: str, color: str, status_url: str = None, logo_data: str = None, dark_mode: str = "auto") -> str:
    status_btn = f"""
    <a class="status-link" href="{status_url}" target="_blank" rel="noopener noreferrer">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="22 12 16 12 13 21 11 3 8 12 2 12"/></svg>
      View status page
      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="7" y1="17" x2="17" y2="7"/><polyline points="7 7 17 7 17 17"/></svg>
    </a>""" if status_url else ""
    visual_block = _render_visual_block(
        color,
        f"""<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
        <polyline points="16 16 12 12 8 16"/>
        <line x1="12" y1="12" x2="12" y2="21"/>
        <path d="M20.39 18.39A5 5 0 0 0 18 9h-1.26A8 8 0 1 0 3 16.3"/>
      </svg>""",
        logo_data,
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="refresh" content="30">
  <title>{title}</title>
  <style>{_base_css(color, "1.6s", dark_mode)}</style>
</head>
<body>
  <div class="card">
    {visual_block}
    <div class="badge"><span class="spinner"></span> Updating</div>
    <h1>{title}</h1>
    <p class="msg">{message}</p>
    <div class="track"><div class="bar"></div></div>
    {status_btn}
    <div class="footer">Powered by Cloudbase &middot; Page auto-refreshes every 30 seconds.</div>
  </div>
</body>
</html>
"""





# â"€â"€ Nginx config generation â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

def generate_config(
    app_name: str,
    domain: str,
    port: int | list,
    ssl_cert: str = None,
    ssl_key: str = None,
    app_id: int = None,
    mode: str = "normal",
    extra_domains: list = None,
    redirect_domains: list = None,
    strict_hostnames: bool = False,
) -> str:
    """Generate an nginx server block.

    port: single int for legacy single-instance proxy, or list[str] of "host:port" backends
          for multi-replica load balancing (nginx upstream block).

    mode:
      'normal'       -  proxy to app; 502/503 automatically serve downtime.html
      'maintenance'  -  serve downtime.html statically (app bypassed)
      'update'       -  serve update.html statically (app bypassed)
      'restart'      -  serve restart.html statically (app bypassed)
      'starting'     -  serve starting.html statically (app bypassed)

    extra_domains:    list of additional domains/subdomains served by the same app
    redirect_domains: list of domains that issue a 301 redirect to the primary domain
    """
    import re as _re

    domain = _normalize_domain(domain)
    extra_domains = [
      d for d in (_normalize_domain(v) for v in (extra_domains or []))
      if d and d != domain
    ]
    redirect_domains = [
      d for d in (_normalize_domain(v) for v in (redirect_domains or []))
      if d and d != domain
    ]

    # Auto-subdomain: if base_domain is configured, include {slug}.{base_domain}
    # as the primary domain (when no custom domain is set) or as an extra server_name.
    import system_config as _scfg
    _using_auto_sub = False
    _base = _scfg.get_base_domain_cached()
    if _base and (app_name or "").strip().lower() != "cloudbase":
        _slug = _re.sub(r"[^a-z0-9]+", "-", (app_name or "").lower()).strip("-")
        if _slug:
            _auto_sub = f"{_slug}.{_base}"
            if not domain:
                domain = _auto_sub
                _using_auto_sub = True
            elif _auto_sub != domain and _auto_sub not in extra_domains:
                extra_domains = list(extra_domains) + [_auto_sub]

    # When using auto-subdomain with no explicit SSL, apply base SSL (wildcard cert)
    if _using_auto_sub and not ssl_cert and not ssl_key:
        _base_cert = _scfg.get_base_ssl_cert_cached()
        _base_key  = _scfg.get_base_ssl_key_cached()
        if _base_cert and _base_key:
            ssl_cert = _base_cert
            ssl_key  = _base_key

    if not domain:
      domain = "localhost"

    ssl_cert = _sanitize_ssl_path(ssl_cert)
    ssl_key = _sanitize_ssl_path(ssl_key)
    if bool(ssl_cert) != bool(ssl_key):
      # Only enable SSL when both paths are valid.
      ssl_cert = None
      ssl_key = None

    is_cloudbase = (app_name or "").strip().lower() == "cloudbase"
    maint_root = f"{MAINTENANCE_DIR}/{app_id}" if app_id else f"{MAINTENANCE_DIR}/0"
    fallback_filename = "downtime.html"
    if is_cloudbase and not app_id:
      maint_root = f"{MAINTENANCE_DIR}/cloudbase"
      fallback_filename = "unavailable.html"

    if mode == "maintenance":
      return _static_page_config(domain, maint_root, "downtime.html", ssl_cert, ssl_key, extra_domains, redirect_domains, strict_hostnames=strict_hostnames)
    if mode == "update":
      return _static_page_config(domain, maint_root, "update.html", ssl_cert, ssl_key, extra_domains, redirect_domains, strict_hostnames=strict_hostnames)
    if mode == "restart":
      return _static_page_config(domain, maint_root, "restart.html", ssl_cert, ssl_key, extra_domains, redirect_domains, strict_hostnames=strict_hostnames)
    if mode == "starting":
      return _static_page_config(domain, maint_root, "starting.html", ssl_cert, ssl_key, extra_domains, redirect_domains, strict_hostnames=strict_hostnames)

    # list[str] of "host:port" backends (instance-based model)
    if isinstance(port, list):
        if not port:
            # No running instances — serve 503 maintenance page
          return _static_page_config(domain, maint_root, "downtime.html", ssl_cert, ssl_key, extra_domains, redirect_domains, strict_hostnames=strict_hostnames)
        if len(port) == 1:
          return _proxy_config(domain, f"http://{port[0]}", maint_root, ssl_cert, ssl_key, extra_domains, redirect_domains, fallback_filename=fallback_filename, strict_hostnames=strict_hostnames)
        safe_name = _re.sub(r"[^a-z0-9_]", "_", app_name.lower())
        upstream_name = f"cloudbase_{safe_name}"
        upstream_block = f"upstream {upstream_name} {{\n"
        for backend in port:
            upstream_block += f"    server {backend};\n"
        upstream_block += "}\n\n"
        return _proxy_config(domain, f"http://{upstream_name}", maint_root, ssl_cert, ssl_key, extra_domains, redirect_domains, upstream_block=upstream_block, fallback_filename=fallback_filename, strict_hostnames=strict_hostnames)

    # Legacy single int port
    return _proxy_config(domain, f"http://127.0.0.1:{port}", maint_root, ssl_cert, ssl_key, extra_domains, redirect_domains, fallback_filename=fallback_filename, strict_hostnames=strict_hostnames)


def _build_strict_host_guard(all_domains: list[str]) -> str:
  import re as _re
  names = [d for d in (all_domains or []) if d]
  if not names:
    return ""
  patterns = []
  for name in names:
    esc = _re.escape(name)
    esc = esc.replace(r"\*", "[^.]+")
    patterns.append(esc)
  host_pattern = "|".join(patterns)
  return f"""
  if ($host !~* ^(?:{host_pattern})$) {{
    return 404;
  }}"""


def _proxy_config(domain: str, proxy_target: str, maint_root: str, ssl_cert: str = None, ssl_key: str = None, extra_domains: list = None, redirect_domains: list = None, upstream_block: str = "", fallback_filename: str = "downtime.html", strict_hostnames: bool = False) -> str:
    # NOTE: proxy_intercept_errors must be inside the proxying location block.
    # We use a regular 'internal' location (not named @) so that try_files works.
    # Named locations don't support try_files, which caused the file to not be served.
    unknown_host_block = """\
    error_page 404 = /_cloudbase_unknown_host;
    location = /_cloudbase_unknown_host {
      internal;
      root /var/www/cloudbase/maintenance/cloudbase;
      try_files /app-not-found.html =404;
      default_type text/html;
      add_header Cache-Control "no-store, no-cache, must-revalidate" always;
      add_header Pragma "no-cache" always;
    }

  """ if strict_hostnames else ""
    server_content = f"""\
  {unknown_host_block}
    # Auto-serve downtime page when upstream returns 502/503/504.
    error_page 502 503 504 =503 /_pdm_maintenance;
    location = /_pdm_maintenance {{
        internal;
        root {maint_root};
        try_files /{fallback_filename} =503;
        default_type text/html;
        add_header Cache-Control "no-store, no-cache, must-revalidate" always;
        add_header Pragma "no-cache" always;
    }}

    location / {{
        proxy_pass {proxy_target};
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_cache_bypass $http_upgrade;
        proxy_intercept_errors on;
    }}"""

    # Build server_name lists
    all_domains = [domain] + [d for d in (extra_domains or []) if d and d != domain]
    server_name_str = " ".join(all_domains)
    strict_guard = _build_strict_host_guard(all_domains) if strict_hostnames else ""
    # Redirect block for domains that should 301 to the primary
    redirect_blocks = _redirect_server_blocks(redirect_domains or [], domain, ssl_cert, ssl_key)

    if ssl_cert and ssl_key:
        return f"""{upstream_block}{redirect_blocks}server {{
    listen 80;
    server_name {server_name_str};
    return 301 https://$host$request_uri;
}}

server {{
    listen 443 ssl;
    server_name {server_name_str};

    ssl_certificate "{ssl_cert}";
    ssl_certificate_key "{ssl_key}";
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
  {strict_guard}

{server_content}
}}
"""
    return f"""{upstream_block}{redirect_blocks}server {{
    listen 80;
    server_name {server_name_str};
  {strict_guard}

{server_content}
}}
"""


def write_cloudbase_unavailable_page(html: str) -> tuple[bool, str]:
  """Write /var/www/cloudbase/maintenance/cloudbase/unavailable.html."""
  cloudbase_dir = os.path.join(MAINTENANCE_DIR, "cloudbase")
  page_path = os.path.join(cloudbase_dir, "unavailable.html")
  try:
    r = subprocess.run(["sudo", "mkdir", "-p", cloudbase_dir], capture_output=True, text=True)
    if r.returncode != 0:
      return False, r.stderr or "Failed to create Cloudbase maintenance directory"

    r = subprocess.run(["sudo", "tee", page_path], input=html, text=True, capture_output=True)
    if r.returncode != 0:
      return False, r.stderr or "Failed to write unavailable page"

    r = subprocess.run(["sudo", "chmod", "644", page_path], capture_output=True, text=True)
    if r.returncode != 0:
      return False, r.stderr or "Failed to chmod unavailable page"
    return True, "OK"
  except Exception as exc:
    log.exception("[cloudbase-unavailable] unexpected error: %s", exc)
    return False, str(exc)


def write_cloudbase_unknown_host_page(html: str) -> tuple[bool, str]:
  """Write /var/www/cloudbase/maintenance/cloudbase/app-not-found.html."""
  cloudbase_dir = os.path.join(MAINTENANCE_DIR, "cloudbase")
  page_path = os.path.join(cloudbase_dir, "app-not-found.html")
  try:
    r = subprocess.run(["sudo", "mkdir", "-p", cloudbase_dir], capture_output=True, text=True)
    if r.returncode != 0:
      return False, r.stderr or "Failed to create Cloudbase maintenance directory"

    r = subprocess.run(["sudo", "tee", page_path], input=html, text=True, capture_output=True)
    if r.returncode != 0:
      return False, r.stderr or "Failed to write unknown-host page"

    r = subprocess.run(["sudo", "chmod", "644", page_path], capture_output=True, text=True)
    if r.returncode != 0:
      return False, r.stderr or "Failed to chmod unknown-host page"
    return True, "OK"
  except Exception as exc:
    log.exception("[cloudbase-unknown-host] unexpected error: %s", exc)
    return False, str(exc)


def _static_page_config(domain: str, maint_root: str, filename: str, ssl_cert: str = None, ssl_key: str = None, extra_domains: list = None, redirect_domains: list = None, strict_hostnames: bool = False) -> str:
    # Serve a single static HTML file with a real 503 status.
    # error_page 503 points to an internal location that reads the file;
    # the outer location just triggers the 503 unconditionally.
    unknown_host_block = """\
    error_page 404 = /_cloudbase_unknown_host;
    location = /_cloudbase_unknown_host {
      internal;
      root /var/www/cloudbase/maintenance/cloudbase;
      try_files /app-not-found.html =404;
      default_type text/html;
      add_header Cache-Control "no-store, no-cache, must-revalidate" always;
      add_header Pragma "no-cache" always;
    }

  """ if strict_hostnames else ""
    server_content = f"""\
  {unknown_host_block}
    root {maint_root};

    error_page 503 /_pdm_static;
    location = /_pdm_static {{
        internal;
        try_files /{filename} =503;
        default_type text/html;
        add_header Cache-Control "no-store, no-cache, must-revalidate" always;
        add_header Pragma "no-cache" always;
    }}

    location / {{
        return 503;
    }}"""

    all_domains = [domain] + [d for d in (extra_domains or []) if d and d != domain]
    server_name_str = " ".join(all_domains)
    strict_guard = _build_strict_host_guard(all_domains) if strict_hostnames else ""
    redirect_blocks = _redirect_server_blocks(redirect_domains or [], domain, ssl_cert, ssl_key)

    if ssl_cert and ssl_key:
        return f"""{redirect_blocks}server {{
    listen 80;
    server_name {server_name_str};
    return 301 https://$host$request_uri;
}}

server {{
    listen 443 ssl;
    server_name {server_name_str};

    ssl_certificate "{ssl_cert}";
    ssl_certificate_key "{ssl_key}";
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
  {strict_guard}

{server_content}
}}
"""
    return f"""{redirect_blocks}server {{
    listen 80;
    server_name {server_name_str};
  {strict_guard}

{server_content}
}}
"""


def _redirect_server_blocks(redirect_domains: list, primary_domain: str, ssl_cert: str = None, ssl_key: str = None) -> str:
    """Generate server blocks that 301-redirect each domain in redirect_domains to primary_domain."""
    if not redirect_domains:
        return ""
    names = " ".join(d for d in redirect_domains if d)
    if not names:
        return ""
    target = f"https://{primary_domain}$request_uri" if ssl_cert and ssl_key else f"http://{primary_domain}$request_uri"
    if ssl_cert and ssl_key:
        return f"""server {{
    listen 80;
    server_name {names};
    return 301 https://{primary_domain}$request_uri;
}}

server {{
    listen 443 ssl;
    server_name {names};

    ssl_certificate "{ssl_cert}";
    ssl_certificate_key "{ssl_key}";
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    return 301 {target};
}}

"""
    return f"""server {{
    listen 80;
    server_name {names};
    return 301 {target};
}}

"""


def _build_default_catch_all_config(ssl_cert: str | None = None, ssl_key: str | None = None) -> str:
  ssl_cert = _sanitize_ssl_path(ssl_cert)
  ssl_key = _sanitize_ssl_path(ssl_key)
  has_ssl = bool(ssl_cert and ssl_key)

  https_block = f"""
server {{
  listen 443 ssl default_server;
  listen [::]:443 ssl default_server;
  server_name _;

  ssl_certificate \"{ssl_cert}\";
  ssl_certificate_key \"{ssl_key}\";
  ssl_protocols TLSv1.2 TLSv1.3;
  ssl_ciphers HIGH:!aNULL:!MD5;

  root /var/www/cloudbase/maintenance/cloudbase;
  error_page 404 = /app-not-found.html;
  location = /app-not-found.html {{
    internal;
    try_files /app-not-found.html =404;
    default_type text/html;
    add_header Cache-Control \"no-store, no-cache, must-revalidate\" always;
    add_header Pragma \"no-cache\" always;
  }}

  location / {{
    return 404;
  }}
}}
""" if has_ssl else ""

  return f"""\
# Cloudbase default catch-all — serves a branded page for unknown hostnames.
# This prevents Nginx from forwarding traffic intended for other services
# (e.g. a hosting control panel) to a random app config.
server {{
  listen 80 default_server;
  listen [::]:80 default_server;
  server_name _;

  root /var/www/cloudbase/maintenance/cloudbase;
  error_page 404 = /app-not-found.html;
  location = /app-not-found.html {{
    internal;
    try_files /app-not-found.html =404;
    default_type text/html;
    add_header Cache-Control \"no-store, no-cache, must-revalidate\" always;
    add_header Pragma \"no-cache\" always;
  }}

  location / {{
    return 404;
  }}
}}
{https_block}
"""


def write_default_catch_all(ssl_cert: str | None = None, ssl_key: str | None = None) -> tuple[bool, str]:
  """Write a default_server block for unmatched requests with a branded 404 page.
  Includes HTTPS default_server when a valid cert/key pair is provided."""
  config_path = os.path.join(NGINX_SITES_DIR, "cloudbase-default")
  enabled_path = os.path.join(NGINX_ENABLED_DIR, "cloudbase-default")
  try:
    catch_all_cfg = _build_default_catch_all_config(ssl_cert, ssl_key)
    r = subprocess.run(["sudo", "tee", config_path], input=catch_all_cfg, text=True, capture_output=True)
    if r.returncode != 0:
      return False, r.stderr or "Failed to write default catch-all"
    subprocess.run(["sudo", "ln", "-sf", config_path, enabled_path], capture_output=True)
    result = subprocess.run(["sudo", "nginx", "-t"], capture_output=True, text=True)
    if result.returncode != 0:
      return False, result.stderr
    subprocess.run(["sudo", "systemctl", "reload", "nginx"], capture_output=True)
    return True, "OK"
  except Exception as e:
    return False, str(e)


def _safe_name(app_name: str) -> str:
    """Convert app name to a valid filename (replace spaces/special chars)."""
    import re
    return re.sub(r'[^a-zA-Z0-9_-]', '_', app_name).lower()


def get_config_path(app_name: str) -> str:
    return os.path.join(NGINX_SITES_DIR, _safe_name(app_name))


def config_uses_restart_page(content: str) -> bool:
    return "try_files /restart.html" in (content or "")



def write_maintenance_files(app_id: int, downtime_html: str, update_html: str, restart_html: str = None, starting_html: str = None) -> tuple[bool, str]:
    """Write downtime.html, update.html (and optionally restart.html, starting.html) to /var/www/cloudbase/maintenance/{app_id}/."""
    app_dir = os.path.join(MAINTENANCE_DIR, str(app_id))
    log.info("[maint-files] writing to %s", app_dir)
    try:
        r = subprocess.run(["sudo", "mkdir", "-p", app_dir], capture_output=True, text=True)
        log.info("[maint-files] mkdir rc=%d stderr=%r", r.returncode, r.stderr)
        if r.returncode != 0:
            return False, r.stderr or "Failed to create maintenance directory"

        files = [("downtime.html", downtime_html), ("update.html", update_html)]
        if restart_html is not None:
            files.append(("restart.html", restart_html))
        if starting_html is not None:
            files.append(("starting.html", starting_html))

        for filename, content in files:
            path = os.path.join(app_dir, filename)
            r = subprocess.run(["sudo", "tee", path], input=content, text=True, capture_output=True)
            log.info("[maint-files] tee %s rc=%d stderr=%r", path, r.returncode, r.stderr)
            if r.returncode != 0:
                return False, r.stderr or f"Failed to write {filename}"

        for filename, _ in files:
            path = os.path.join(app_dir, filename)
            r = subprocess.run(["sudo", "chmod", "644", path], capture_output=True, text=True)
            log.info("[maint-files] chmod %s rc=%d stderr=%r", path, r.returncode, r.stderr)
            if r.returncode != 0:
                return False, r.stderr or f"Failed to chmod {filename}"
        return True, "OK"
    except Exception as exc:
        log.exception("[maint-files] unexpected error: %s", exc)
        return False, str(exc)


def _disable_broken_configs(current_safe: str, nginx_stderr: str) -> bool:
    """Disable any sites-enabled config (other than current_safe) that references a missing cert/key file.
    Returns True if at least one broken config was disabled."""
    import re
    # Parse paths nginx complains about (cert or key files)
    bad_paths = set(re.findall(r'(?:cannot load certificate|cannot load certificate key)["\s]+\"([^\"]+)\"', nginx_stderr))
    if not bad_paths:
        # Broader fallback: any quoted path in the error
        bad_paths = set(re.findall(r'"(/[^"]+)"', nginx_stderr))

    disabled_any = False
    try:
        enabled_dir = NGINX_ENABLED_DIR
        for entry in os.listdir(enabled_dir):
            if entry == current_safe:
                continue
            symlink = os.path.join(enabled_dir, entry)
            try:
                with open(symlink) as f:
                    content = f.read()
            except Exception:
                continue
            if any(p in content for p in bad_paths):
                r = subprocess.run(["sudo", "rm", "-f", symlink], capture_output=True)
                if r.returncode == 0:
                    log.warning("[nginx-cfg] disabled broken config %r (referenced missing file)", entry)
                    disabled_any = True
    except Exception as e:
        log.warning("[nginx-cfg] _disable_broken_configs error: %s", e)
    return disabled_any


def write_nginx_config(app_name: str, config: str) -> tuple[bool, str]:
    safe = _safe_name(app_name)
    config_path = os.path.join(NGINX_SITES_DIR, safe)
    enabled_path = os.path.join(NGINX_ENABLED_DIR, safe)
    cfg = config or ""
    cfg_sha = hashlib.sha256(cfg.encode("utf-8", errors="ignore")).hexdigest()[:12]
    upstream_count = cfg.count("upstream ")
    server_block_count = cfg.count("\nserver {")
    line_count = cfg.count("\n") + (1 if cfg else 0)
    cfg_bytes = len(cfg.encode("utf-8", errors="ignore"))
    log.info("[nginx-cfg] writing config for app=%r safe=%r path=%s", app_name, safe, config_path)
    log.info(
        "[nginx-cfg] app=%r sha=%s lines=%d upstreams=%d server_blocks=%d bytes=%d",
        app_name,
        cfg_sha,
        line_count,
        upstream_count,
        server_block_count,
        cfg_bytes,
    )

    try:
        # Write via sudo tee (works without direct write permission)
        result = subprocess.run(
            ["sudo", "tee", config_path],
            input=config, text=True, capture_output=True,
        )
        log.info("[nginx-cfg] tee config rc=%d stderr=%r", result.returncode, result.stderr)
        if result.returncode != 0:
            return False, result.stderr or "Failed to write nginx config"

        # Symlink into sites-enabled
        if not os.path.exists(enabled_path):
            r = subprocess.run(
                ["sudo", "ln", "-sf", config_path, enabled_path],
                capture_output=True, text=True,
            )
            log.info("[nginx-cfg] symlink rc=%d stderr=%r", r.returncode, r.stderr)
            if r.returncode != 0:
                return False, r.stderr or "Failed to enable nginx site"
        else:
            log.info("[nginx-cfg] symlink already exists at %s", enabled_path)
            # Always re-create symlink to ensure it points to current config
            subprocess.run(["sudo", "ln", "-sf", config_path, enabled_path], capture_output=True)

        # Validate config — if a *different* app's broken config causes the failure,
        # disable it automatically and retry once.
        result = subprocess.run(["sudo", "nginx", "-t"], capture_output=True, text=True)
        log.info("[nginx-cfg] nginx -t rc=%d stdout=%r stderr=%r", result.returncode, result.stdout, result.stderr)
        if result.returncode != 0:
            disabled = _disable_broken_configs(safe, result.stderr)
            if disabled:
                result = subprocess.run(["sudo", "nginx", "-t"], capture_output=True, text=True)
                log.info("[nginx-cfg] nginx -t retry rc=%d stderr=%r", result.returncode, result.stderr)
            if result.returncode != 0:
                return False, result.stderr

        r = subprocess.run(["sudo", "systemctl", "reload", "nginx"], capture_output=True, text=True)
        log.info("[nginx-cfg] reload rc=%d stderr=%r", r.returncode, r.stderr)
        return True, "OK"
    except FileNotFoundError:
        log.error("[nginx-cfg] nginx not found")
        return False, "nginx not found  -  install nginx first (sudo apt install nginx)"
    except Exception as e:
        log.exception("[nginx-cfg] unexpected error")
        return False, str(e)


def remove_nginx_config(app_name: str) -> bool:
    safe = _safe_name(app_name)
    config_path = os.path.join(NGINX_SITES_DIR, safe)
    enabled_path = os.path.join(NGINX_ENABLED_DIR, safe)

    removed = False
    for path in [enabled_path, config_path]:
        r = subprocess.run(["sudo", "rm", "-f", path], capture_output=True)
        if r.returncode == 0:
            removed = True

    if removed:
        subprocess.run(["sudo", "systemctl", "reload", "nginx"], capture_output=True)
    return removed
