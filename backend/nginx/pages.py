from core import paths

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
  path = paths.LOGO_FILE
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
    p a {{ color: var(--text); text-underline-offset: 3px; }}
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


def generate_cloudbase_node_html(primary_url: str | None = None, node_name: str | None = None) -> str:
    """Shown on a node-only server, which runs instances for a primary but has no panel."""
    esc = lambda v: (v or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    safe_url, safe_name = esc(primary_url), esc(node_name)
    where = f'<a href="{safe_url}">{safe_url}</a>' if safe_url else "the primary’s panel"
    return _cloudbase_status_page(
        title="This server is a node",
        message=f"It runs instances for a Cloudbase primary and has no panel of its own. Manage its apps from {where}.",
        meta=f"Node{f' · {safe_name}' if safe_name else ''}",
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
