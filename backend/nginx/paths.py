import os

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


def _safe_name(app_name: str) -> str:
    """Convert app name to a valid filename (replace spaces/special chars)."""
    import re
    return re.sub(r'[^a-zA-Z0-9_-]', '_', app_name).lower()


def get_config_path(app_name: str) -> str:
    return os.path.join(NGINX_SITES_DIR, _safe_name(app_name))


# nginx loads sites-enabled alphabetically and the first server_name wins, so
# "zz-" makes an app's own server block always take precedence over this one.
ACME_CONFIG_NAME = "zz-cloudbase-acme"
