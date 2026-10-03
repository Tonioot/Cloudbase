import logging
import os
import subprocess
import hashlib

log = logging.getLogger("pdm.nginx")

NGINX_SITES_DIR = "/etc/nginx/sites-available"
NGINX_ENABLED_DIR = "/etc/nginx/sites-enabled"
MAINTENANCE_DIR = "/var/www/cloudbase/maintenance"
ACME_WEBROOT = "/var/www/cloudbase/acme"

# Served on every port-80 server so Let's Encrypt can verify a domain
# (HTTP-01), also while the rest of that server redirects to HTTPS.
_ACME_LOCATION = f"""
    location ^~ /.well-known/acme-challenge/ {{
        root {ACME_WEBROOT};
        default_type text/plain;
        try_files $uri =404;
    }}
"""


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
    theme: str = "auto",
    brand_name: str = None,
    background: str = "none",
    preview: bool = False,
) -> str:
    """Return a full HTML page for one of an app's visitor pages. Uses custom_html if provided."""
    if custom_html:
        return custom_html

    import re as _re
    def esc(v):
        return (v or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    safe_color = color if color and _re.match(r"^#(?:[0-9a-fA-F]{3}){1,2}$", color) else "#f85149"
    safe_status_url = esc(status_url) if status_url and _re.match(r"^https?://", status_url) else None
    # Logo must be a data URL with an image MIME type
    safe_logo_data = logo_data if logo_data and _re.match(r"^data:image/[a-zA-Z0-9+/.-]+;base64,[A-Za-z0-9+/=]+$", logo_data) else None

    return _app_status_page(
        kind=page_type if page_type in _APP_PAGE_KINDS else "update",
        title=esc(title),
        message=esc(message),
        color=safe_color,
        status_url=safe_status_url,
        logo_data=safe_logo_data,
        theme=theme if theme in ("light", "dark") else "auto",
        brand_name=esc((brand_name or "").strip()[:60]),
        background=background if background in ("glow", "grid") else "none",
        preview=preview,
    )


def _cloudbase_logo_data_uri() -> str | None:
  """The Cloudbase logo as a small inline data URI (the full-size PNG is ~780 KB)."""
  import base64
  path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "cloudbase-mark.png")
  try:
    with open(path, "rb") as f:
      return "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")
  except OSError:
    return None


def _cloudbase_status_page(title: str, message: str, meta: str, tone: str, refresh_seconds: int | None = None) -> str:
    """Minimal Cloudbase-branded status page (light/dark via prefers-color-scheme).

    Served by nginx as a static file while the panel may be down, so everything
    is inline: no fonts, scripts or images are fetched.
    """
    refresh = f'<meta http-equiv="refresh" content="{refresh_seconds}">' if refresh_seconds else ""
    logo = _cloudbase_logo_data_uri()
    mark = (
        f'<img class="mark" src="{logo}" alt="Cloudbase">' if logo else
        '<div class="mark mark-fallback" aria-hidden="true"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M7 18a4.5 4.5 0 0 1-.6-8.96A6 6 0 0 1 18 8.5a4 4 0 0 1-.5 9.5z"/></svg></div>'
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  {refresh}
  <title>{title} — Cloudbase</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #08090a; --text: #edeef0; --text-2: #a9aeb6; --muted: #7a7f88; --line: #17191c;
      --tone: {tone}; --halo: color-mix(in srgb, {tone} 18%, transparent);
    }}
    @media (prefers-color-scheme: light) {{
      :root {{ color-scheme: light; --bg: #fbfbfa; --text: #111214; --text-2: #4a4f57; --muted: #6b7079; --line: #eaeae8; }}
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px 16px;
      background: var(--bg);
      color: var(--text);
      font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      -webkit-font-smoothing: antialiased;
    }}
    main {{ width: 100%; max-width: 420px; }}
    .mark {{ display: block; width: 44px; height: 44px; margin-bottom: 32px; object-fit: contain; }}
    .mark-fallback {{
      width: 36px; height: 36px; border-radius: 10px; background: var(--text); color: var(--bg);
      display: flex; align-items: center; justify-content: center;
    }}
    .status {{ display: flex; align-items: center; gap: 10px; margin-bottom: 14px; font-size: 13px; color: var(--text-2); }}
    .dot {{ width: 7px; height: 7px; border-radius: 50%; background: var(--tone); box-shadow: 0 0 0 4px var(--halo); }}
    h1 {{ font-size: 28px; font-weight: 400; line-height: 1.15; letter-spacing: -0.03em; margin-bottom: 12px; }}
    p {{ font-size: 14px; line-height: 1.6; color: var(--text-2); }}
    .meta {{
      margin-top: 28px; padding-top: 16px; border-top: 1px solid var(--line);
      font-family: ui-monospace, 'SFMono-Regular', Menlo, Consolas, monospace; font-size: 12px; color: var(--muted);
    }}
  </style>
</head>
<body>
  <main>
    {mark}
    <div class="status"><span class="dot"></span>{meta}</div>
    <h1>{title}</h1>
    <p>{message}</p>
  </main>
</body>
</html>
"""


def generate_cloudbase_unavailable_html(domain: str | None = None) -> str:
    """Shown by nginx while the Cloudbase panel itself is restarting or offline."""
    safe_domain = (domain or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _cloudbase_status_page(
        title="Cloudbase is restarting",
        message="The panel is temporarily unavailable while its services come back up. "
                "This page refreshes automatically — your apps keep running.",
        meta=f"Panel offline{f' · {safe_domain}' if safe_domain else ''}",
        tone="#f5b94e",
        refresh_seconds=8,
    )


def generate_cloudbase_unknown_host_html(domain: str | None = None) -> str:
    """Shown for hostnames that point at this server but are not linked to an app."""
    safe_domain = (domain or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _cloudbase_status_page(
        title="Nothing is deployed here",
        message="This hostname points to a Cloudbase server, but it is not linked to any app.",
        meta=f"Unknown host{f' · panel at {safe_domain}' if safe_domain else ''}",
        tone="#7a7f88",
    )


# Defaults for an app's visitor pages when the owner left a field empty.
PAGE_DEFAULTS = {
    "downtime": {"title": "Down for Maintenance", "message": "We'll be back shortly.", "color": "#e5484d"},
    "update": {"title": "Updating…", "message": "We’re deploying a new version. Check back soon.", "color": "#f5a524"},
    "restart": {"title": "Restarting…", "message": "The server is restarting. This only takes a moment.", "color": "#3b82f6"},
    "starting": {"title": "Starting…", "message": "The service is starting up. This only takes a moment.", "color": "#3b82f6"},
}


def render_app_page(page_type: str, cfg: dict | None, preview: bool = False) -> str:
    """HTML for one of an app's visitor pages, filling in defaults for empty fields."""
    cfg = cfg or {}
    d = PAGE_DEFAULTS[page_type]
    return generate_maintenance_html(
        cfg.get("title") or d["title"],
        cfg.get("message") or d["message"],
        cfg.get("color") or d["color"],
        cfg.get("status_url"),
        cfg.get("custom_html"),
        page_type,
        logo_data=cfg.get("logo_data"),
        theme=cfg.get("theme") or "auto",
        brand_name=cfg.get("brand_name"),
        background=cfg.get("background") or "none",
        preview=preview,
    )


# Visitor-facing pages for an app (downtime, update, restart, starting).
# One shared, quiet layout: optional logo, a status line in the app's accent
# colour, title, message and an optional status-page link. Light/dark follows
# the visitor's system setting. Everything is inline — nginx serves the file
# while the app itself may be down.
_APP_PAGE_KINDS = {
    "downtime": {"label": "Temporarily unavailable", "refresh": 30, "check": 15, "busy": False},
    "update":   {"label": "Scheduled maintenance",   "refresh": 30, "check": 10, "busy": True},
    "restart":  {"label": "Restarting",              "refresh": 8,  "check": 4,  "busy": True},
    "starting": {"label": "Starting up",             "refresh": 8,  "check": 4,  "busy": True},
}


def _app_status_page(*, kind: str, title: str, message: str, color: str, status_url: str = None,
                     logo_data: str = None, theme: str = "auto", brand_name: str = "",
                     background: str = "none", preview: bool = False) -> str:
    meta = _APP_PAGE_KINDS[kind]

    # Header bar with the logo and/or brand name, like the site's own navigation
    brand = ""
    if logo_data or brand_name:
        brand = (
            '<header><div class="brand">'
            + (f'<img class="logo" src="{logo_data}" alt="">' if logo_data else "")
            + (f'<span class="brand-name">{brand_name}</span>' if brand_name else "")
            + "</div></header>"
        )
    link = (
        f'<a class="link" href="{status_url}" target="_blank" rel="noopener noreferrer">'
        'View status page'
        '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="7" y1="17" x2="17" y2="7"/><polyline points="7 7 17 7 17 17"/></svg>'
        '</a>'
    ) if status_url else ""
    busy = '<div class="progress" aria-hidden="true"><span></span></div>' if meta["busy"] else ""

    # Live check: request the same URL in the background and reload as soon as
    # it no longer answers with an error (nginx serves these pages as 503).
    # The meta refresh is only a fallback for visitors without JavaScript.
    interval_ms = meta["check"] * 1000
    live_js = "" if preview else f"""
  <script>
    (function () {{
      var el = document.getElementById('check'), last = Date.now(), pending = false;
      function label() {{
        var s = Math.round((Date.now() - last) / 1000);
        el.textContent = 'Checking automatically · last checked ' + (s < 2 ? 'just now' : s + 's ago');
      }}
      function check() {{
        if (pending) return;
        pending = true;
        fetch(location.href, {{ cache: 'no-store', redirect: 'manual' }})
          .then(function (r) {{ if (r.type === 'opaqueredirect' || (r.status > 0 && r.status < 500)) location.reload(); }})
          .catch(function () {{}})
          .then(function () {{ last = Date.now(); pending = false; label(); }});
      }}
      setInterval(check, {interval_ms});
      setInterval(label, 1000);
      label();
    }})();
  </script>"""
    refresh_meta = "" if preview else f'<noscript><meta http-equiv="refresh" content="{meta["refresh"]}"></noscript>'
    check_text = "Preview — automatic checking is off" if preview else "Checking automatically"

    light = "color-scheme: light; --bg: #fbfbfa; --text: #111214; --text-2: #4a4f57; --muted: #7a7f88; --line: #e8e8e5; --grid: rgba(17,18,20,.06);"
    dark = "color-scheme: dark; --bg: #0a0b0c; --text: #edeef0; --text-2: #a9aeb6; --muted: #7a7f88; --line: #1d1f22; --grid: rgba(237,238,240,.06);"
    if theme == "light":
        theme_css = f":root {{ {light} --accent: {color}; }}"
    elif theme == "dark":
        theme_css = f":root {{ {dark} --accent: {color}; }}"
    else:
        theme_css = (f":root {{ {light} --accent: {color}; }}\n"
                     f"    @media (prefers-color-scheme: dark) {{ :root {{ {dark} }} }}")

    if background == "glow":
        bg_css = ("body::before { content: ''; position: fixed; inset: 0; pointer-events: none; z-index: 0;"
                  " background: radial-gradient(55% 45% at 50% 45%, color-mix(in srgb, var(--accent) 16%, transparent), transparent 75%); }")
    elif background == "grid":
        bg_css = ("body::before { content: ''; position: fixed; inset: 0; pointer-events: none; z-index: 0;"
                  " background-image: linear-gradient(var(--grid) 1px, transparent 1px), linear-gradient(90deg, var(--grid) 1px, transparent 1px);"
                  " background-size: 32px 32px; background-position: center;"
                  " -webkit-mask-image: radial-gradient(65% 60% at 50% 45%, #000 25%, transparent 80%);"
                  " mask-image: radial-gradient(65% 60% at 50% 45%, #000 25%, transparent 80%); }")
    else:
        bg_css = ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  {refresh_meta}
  <meta name="robots" content="noindex">
  <title>{title}</title>
  <style>
    {theme_css}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      background: var(--bg);
      color: var(--text);
      font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      -webkit-font-smoothing: antialiased;
    }}
    {bg_css}
    header, .wrap {{ position: relative; z-index: 1; }}
    header {{ display: flex; align-items: center; height: 72px; padding: 0 32px; border-bottom: 1px solid var(--line); background: var(--bg); }}
    .brand {{ display: flex; align-items: center; gap: 10px; min-width: 0; }}
    .logo {{ display: block; height: 28px; max-width: 160px; object-fit: contain; }}
    .brand-name {{ font-size: 16px; font-weight: 600; letter-spacing: -0.01em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    .wrap {{ flex: 1; display: flex; align-items: center; justify-content: center; padding: 48px 20px; }}
    main {{ width: 100%; max-width: 440px; }}
    .status {{ display: flex; align-items: center; gap: 10px; margin-bottom: 16px; font-size: 13px; color: var(--text-2); }}
    .dot {{
      width: 7px; height: 7px; border-radius: 50%; background: var(--accent);
      box-shadow: 0 0 0 4px color-mix(in srgb, var(--accent) 18%, transparent);
    }}
    h1 {{ font-size: 30px; font-weight: 500; line-height: 1.15; letter-spacing: -0.03em; margin-bottom: 14px; }}
    p {{ font-size: 15px; line-height: 1.65; color: var(--text-2); }}
    .progress {{ position: relative; height: 2px; margin-top: 32px; background: var(--line); border-radius: 2px; overflow: hidden; }}
    .progress span {{
      position: absolute; top: 0; bottom: 0; width: 30%; border-radius: 2px; background: var(--accent);
      animation: slide 1.6s ease-in-out infinite;
    }}
    @keyframes slide {{ from {{ left: -30%; }} to {{ left: 100%; }} }}
    .link {{
      display: inline-flex; align-items: center; gap: 6px; margin-top: 28px;
      font-size: 14px; color: var(--text); text-decoration: none;
      border-bottom: 1px solid var(--line); padding-bottom: 2px;
    }}
    .link:hover {{ border-bottom-color: var(--text); }}
    footer {{ margin-top: 32px; font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }}
    @media (max-width: 520px) {{ header {{ height: 60px; padding: 0 20px; }} }}
    @media (prefers-reduced-motion: reduce) {{ .progress span {{ animation: none; left: 0; width: 100%; opacity: .35; }} }}
  </style>
</head>
<body>
  {brand}
  <div class="wrap"><main>
    <div class="status"><span class="dot"></span>{meta["label"]}</div>
    <h1>{title}</h1>
    <p>{message}</p>
    {busy}
    {link}
    <footer id="check">{check_text}</footer>
  </main></div>{live_js}
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
{_ACME_LOCATION}
    location / {{
        return 301 https://$host$request_uri;
    }}
}}

server {{
    listen 443 ssl;
    server_name {server_name_str};

    ssl_certificate "{ssl_cert}";
    ssl_certificate_key "{ssl_key}";
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
  {strict_guard}
{_ACME_LOCATION}
{server_content}
}}
"""
    return f"""{upstream_block}{redirect_blocks}server {{
    listen 80;
    server_name {server_name_str};
  {strict_guard}
{_ACME_LOCATION}
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
{_ACME_LOCATION}
    location / {{
        return 301 https://$host$request_uri;
    }}
}}

server {{
    listen 443 ssl;
    server_name {server_name_str};

    ssl_certificate "{ssl_cert}";
    ssl_certificate_key "{ssl_key}";
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
  {strict_guard}
{_ACME_LOCATION}
{server_content}
}}
"""
    return f"""{redirect_blocks}server {{
    listen 80;
    server_name {server_name_str};
  {strict_guard}
{_ACME_LOCATION}
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
{_ACME_LOCATION}
    location / {{
        return 301 https://{primary_domain}$request_uri;
    }}
}}

server {{
    listen 443 ssl;
    server_name {names};

    ssl_certificate "{ssl_cert}";
    ssl_certificate_key "{ssl_key}";
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
{_ACME_LOCATION}
    location / {{
        return 301 {target};
    }}
}}

"""
    return f"""server {{
    listen 80;
    server_name {names};
{_ACME_LOCATION}
    location / {{
        return 301 {target};
    }}
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
{_ACME_LOCATION}
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


ACME_CONFIG_NAME = "cloudbase-acme"


def write_acme_server(domains: list[str]) -> tuple[bool, str]:
    """Temporary port-80 server that answers Let's Encrypt challenges for
    domains that aren't linked to an app yet. Removed again after issuing."""
    names = " ".join(_normalize_domain(d) for d in domains if d)
    config = f"""# Cloudbase: temporary server for HTTPS certificate verification
server {{
    listen 80;
    server_name {names};
{_ACME_LOCATION}
    location / {{
        return 404;
    }}
}}
"""
    return write_nginx_config(ACME_CONFIG_NAME, config)


def remove_acme_server() -> None:
    remove_nginx_config(ACME_CONFIG_NAME)


def write_acme_probe(token: str, content: str) -> tuple[bool, str]:
    """Place a file in the challenge webroot (owned by the Cloudbase user)."""
    path = os.path.join(ACME_WEBROOT, ".well-known", "acme-challenge", token)
    try:
        with open(path, "w", encoding="ascii") as f:
            f.write(content)
        return True, path
    except OSError as e:
        return False, str(e)


def remove_acme_probe(token: str) -> None:
    try:
        os.remove(os.path.join(ACME_WEBROOT, ".well-known", "acme-challenge", token))
    except OSError:
        pass


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
