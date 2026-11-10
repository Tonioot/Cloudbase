from core import system_config as _scfg
from nginx.paths import MAINTENANCE_DIR, _ACME_LOCATION, _normalize_domain, _sanitize_ssl_path, subdomain_slug
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
) -> str:
    """Generate an nginx server block.

    port: single int (the panel itself), or list[str] of "host:port" backends
          for multi-instance load balancing (nginx upstream block).

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
    _using_auto_sub = False
    _base = _scfg.get_base_domain_cached()
    if _base and (app_name or "").strip().lower() != "cloudbase":
        _slug = subdomain_slug(app_name)
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
        else:
            # No wildcard certificate: use the app's own automatic certificate
            # for its subdomain once Cloudbase has requested it
            if nginx_certbot.capability()[0] and nginx_certbot.exists(domain):
                ssl_cert, ssl_key = nginx_certbot.paths(domain)

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
        upstream_name = f"cloudbase_app_{app_id}" if app_id else "cloudbase_" + _re.sub(r"[^a-z0-9_]", "_", app_name.lower())
        upstream_block = f"upstream {upstream_name} {{\n"
        for backend in port:
            upstream_block += f"    server {backend};\n"
        upstream_block += "}\n\n"
        return _proxy_config(domain, f"http://{upstream_name}", maint_root, ssl_cert, ssl_key, extra_domains, redirect_domains, upstream_block=upstream_block, fallback_filename=fallback_filename, strict_hostnames=strict_hostnames)

    # One port: the panel itself
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
        proxy_cache_bypass $http_upgrade;
        proxy_intercept_errors on;
        # Declaring error_page here stops the server-level ones from being inherited,
        # so only upstream outages are intercepted. The app's own 404s pass through
        # instead of turning into the unknown-host page meant for the host guard.
        error_page 502 503 504 =503 /_cloudbase_maintenance;
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
