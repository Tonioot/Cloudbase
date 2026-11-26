from core import system_config as _scfg
import os
from nginx.paths import MAINTENANCE_DIR, TRAFFIC_LOG_DIR, _ACME_LOCATION, _normalize_domain, _sanitize_ssl_path, subdomain_slug, traffic_log_path
from nginx import certbot as nginx_certbot

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
    site_root: str | None = None,
    *,
    _auto: bool = True,
    _defs: bool = True,
) -> str:
    """Generate an nginx server block.

    port: single int (the panel itself), or list[str] of "host:port" backends
          for multi-instance load balancing (nginx upstream block).
    site_root: a static site's published folder: nginx serves its files itself (no backend; `port` is not used).

    mode:
      'normal'       -  proxy to app; 502/503 automatically serve downtime.html
      'maintenance'  -  serve downtime.html statically (app bypassed)
      'update'       -  serve update.html statically (app bypassed)
      'restart'      -  serve restart.html statically (app bypassed)
      'starting'     -  serve starting.html statically (app bypassed)

    extra_domains:    list of additional domains/subdomains served by the same app
    redirect_domains: list of domains that issue a 301 redirect to the primary domain

    An app with a domain of its own also answers on its automatic subdomain (<name>.<base domain>). When that subdomain
    has a certificate of its own (the base domain's wildcard, or its own automatic one), it gets a server block of its
    own with that certificate: in the block of the custom domain it would be served with the custom domain's
    certificate, which browsers refuse for it. (_auto, _defs: for that second block, which leaves out the subdomain
    logic and the upstream and log format the first block already defines.)
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
    _using_auto_sub = False
    _base = _scfg.get_base_domain_cached() if _auto else ""
    if _base and (app_name or "").strip().lower() != "cloudbase":
        _slug = subdomain_slug(app_name)
        if _slug:
            _auto_sub = f"{_slug}.{_base}"
            if not domain:
                domain = _auto_sub
                _using_auto_sub = True
            elif _auto_sub != domain and _auto_sub not in extra_domains:
                sub_cert, sub_key = auto_subdomain_certificate(_auto_sub)
                if sub_cert:
                    # A block of its own with its own certificate, after the custom domain's
                    def block(name, names, redirects, cert, key, defs):
                        return generate_config(app_name, name, port, cert, key, app_id, mode, names, redirects,
                                               strict_hostnames, site_root, _auto=False, _defs=defs)
                    return (block(domain, extra_domains, redirect_domains, ssl_cert, ssl_key, _defs) + "\n"
                            + block(_auto_sub, [], [], sub_cert, sub_key, False))
                extra_domains = list(extra_domains) + [_auto_sub]     # no certificate for it yet: served with the domain

    # When using auto-subdomain with no explicit SSL, apply base SSL (wildcard cert), or its own automatic one
    if _using_auto_sub and not ssl_cert and not ssl_key:
        ssl_cert, ssl_key = auto_subdomain_certificate(domain)

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

    traffic = _traffic_log(app_id) if app_id and not is_cloudbase else ("", "")

    if site_root:
        return _proxy_config(domain, "", maint_root, ssl_cert, ssl_key, extra_domains, redirect_domains,
                             strict_hostnames=strict_hostnames, traffic=traffic, site_root=site_root, defs=_defs)

    # list[str] of "host:port" backends (instance-based model)
    if isinstance(port, list):
        if not port:
            # No running instances — serve 503 maintenance page
          return _static_page_config(domain, maint_root, "downtime.html", ssl_cert, ssl_key, extra_domains, redirect_domains, strict_hostnames=strict_hostnames)
        if len(port) == 1:
          return _proxy_config(domain, f"http://{port[0]}", maint_root, ssl_cert, ssl_key, extra_domains, redirect_domains, fallback_filename=fallback_filename, strict_hostnames=strict_hostnames, traffic=traffic, defs=_defs)
        upstream_name = f"cloudbase_app_{app_id}" if app_id else "cloudbase_" + _re.sub(r"[^a-z0-9_]", "_", app_name.lower())
        upstream_block = f"upstream {upstream_name} {{\n"
        for backend in port:
            # One failed connection takes a backend out for a few seconds, so a dead node costs a request one short
            # connect timeout (below) and then no request at all, instead of one in N waiting a minute
            upstream_block += f"    server {backend} max_fails=1 fail_timeout=5s;\n"
        upstream_block += "}\n\n"
        return _proxy_config(domain, f"http://{upstream_name}", maint_root, ssl_cert, ssl_key, extra_domains, redirect_domains, upstream_block=upstream_block, fallback_filename=fallback_filename, strict_hostnames=strict_hostnames, traffic=traffic, defs=_defs)

    # One port: the panel itself
    return _proxy_config(domain, f"http://127.0.0.1:{port}", maint_root, ssl_cert, ssl_key, extra_domains, redirect_domains, fallback_filename=fallback_filename, strict_hostnames=strict_hostnames, panel=True)


def auto_subdomain_certificate(name: str) -> tuple[str | None, str | None]:
    """(cert, key) for an app's automatic subdomain: the base domain's wildcard certificate, or else the subdomain's
    own automatic one once Cloudbase has requested it; (None, None) without one."""
    base_cert, base_key = _scfg.get_base_ssl_cert_cached(), _scfg.get_base_ssl_key_cached()
    if base_cert and base_key:
        return base_cert, base_key
    if nginx_certbot.capability()[0] and nginx_certbot.exists(name):
        return nginx_certbot.paths(name)
    return None, None


def _traffic_log(app_id: int) -> tuple[str, str]:
    """(at the top of the file, in the proxying location): log what nginx sends to each visitor of the app, as one
    number per request. A format of its own per app, so each file can define its own (the files are included in
    alphabetical order, so a shared one could come after its first use). nginx's own access log keeps its lines."""
    if not os.path.isdir(TRAFFIC_LOG_DIR):
        return "", ""
    name = f"cloudbase_traffic_{int(app_id)}"
    return (f"log_format {name} '$bytes_sent';\n\n",
            f"""
        access_log {traffic_log_path(app_id)} {name} buffer=16k flush=10s;
        access_log /var/log/nginx/access.log;""")


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


def _site_location(site_root: str, traffic_lines: str) -> str:
    """A static site from disk. Only files under its own folder are served: no symlinks (disable_symlinks: the files are
    copied without any, this is the second lock), no hidden files except .well-known, and nothing but plain files."""
    if any(ch in site_root for ch in ('"', "'", ";", " ", "\n", "\r", "{", "}", "$")):
        raise ValueError("Unsafe static site folder")
    return f"""
    root {site_root};
    index index.html index.htm;
    disable_symlinks on;
    autoindex off;
    gzip on;
    gzip_vary on;
    gzip_types text/plain text/css text/xml application/javascript application/json application/xml image/svg+xml;
    location ~ /\\.(?!well-known/) {{
        return 404;
    }}

    location / {{
        # The file, the folder's index, the page without .html, else the site's own index.html (single-page apps)
        try_files $uri $uri/ $uri.html /index.html =404;
        add_header X-Content-Type-Options nosniff always;{traffic_lines}
    }}"""


def _proxy_config(domain: str, proxy_target: str, maint_root: str, ssl_cert: str = None, ssl_key: str = None, extra_domains: list = None, redirect_domains: list = None, upstream_block: str = "", fallback_filename: str = "downtime.html", strict_hostnames: bool = False, panel: bool = False, traffic: tuple[str, str] = ("", ""), site_root: str | None = None, defs: bool = True) -> str:
    if not defs:                    # a second block of the same app: the first one defines its upstream and log format
        upstream_block, traffic = "", ("", traffic[1])
    upstream_block = traffic[0] + upstream_block
    # The panel receives uploads from nodes (database backups, app sources) that are far larger than the 1 MB nginx
    # accepts by default, which answered 413. No limit here; the panel streams the body to disk itself.
    body_limit = """
        client_max_body_size 0;
        proxy_request_buffering off;
        proxy_send_timeout 1h;
        proxy_read_timeout 1h;""" if panel else ""
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
    error_page 502 503 504 =503 /_cloudbase_maintenance;
    location = /_cloudbase_maintenance {{
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
        proxy_cache_bypass $http_upgrade;{body_limit}{traffic[1]}
        # A backend that does not even accept the connection (a node that is gone) fails fast: the default wait is
        # 60 s. With several instances the request goes to the next one (only after errors and timeouts of the
        # connection, never after the app answered), within a few seconds at most.
        proxy_connect_timeout 3s;
        proxy_next_upstream error timeout;
        proxy_next_upstream_timeout 8s;
        proxy_intercept_errors on;
        # Declaring error_page here stops the server-level ones from being inherited,
        # so only upstream outages are intercepted. The app's own 404s pass through
        # instead of turning into the unknown-host page meant for the host guard.
        error_page 502 503 504 =503 /_cloudbase_maintenance;
    }}"""
    if site_root:
        server_content = f"""\
  {unknown_host_block}{_site_location(site_root, traffic[1])}"""

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

    error_page 503 /_cloudbase_static;
    location = /_cloudbase_static {{
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
