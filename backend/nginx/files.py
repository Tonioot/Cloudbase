import logging
import os
import subprocess
import hashlib
from nginx.config import _build_default_catch_all_config
from nginx.paths import ACME_CONFIG_NAME, ACME_WEBROOT, MAINTENANCE_DIR, NGINX_ENABLED_DIR, NGINX_SITES_DIR, _ACME_LOCATION, _normalize_domain, _safe_name

log = logging.getLogger("cloudbase.nginx")


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


def write_maintenance_files(app_id: int, downtime_html: str, update_html: str, restart_html: str = None, starting_html: str = None) -> tuple[bool, str]:
    """Write downtime.html, update.html (and optionally restart.html, starting.html) to /var/www/cloudbase/maintenance/{app_id}/."""
    app_dir = os.path.join(MAINTENANCE_DIR, str(app_id))
    log.debug("[maint-files] writing to %s", app_dir)
    try:
        r = subprocess.run(["sudo", "mkdir", "-p", app_dir], capture_output=True, text=True)
        log.log(logging.WARNING if r.returncode else logging.DEBUG, "[maint-files] mkdir rc=%d stderr=%r", r.returncode, r.stderr)
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
            log.log(logging.WARNING if r.returncode else logging.DEBUG, "[maint-files] tee %s rc=%d stderr=%r", path, r.returncode, r.stderr)
            if r.returncode != 0:
                return False, r.stderr or f"Failed to write {filename}"

        for filename, _ in files:
            path = os.path.join(app_dir, filename)
            r = subprocess.run(["sudo", "chmod", "644", path], capture_output=True, text=True)
            log.log(logging.WARNING if r.returncode else logging.DEBUG, "[maint-files] chmod %s rc=%d stderr=%r", path, r.returncode, r.stderr)
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
    log.debug("[nginx-cfg] writing config for app=%r safe=%r path=%s", app_name, safe, config_path)
    log.debug(
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
        log.log(logging.WARNING if result.returncode else logging.DEBUG, "[nginx-cfg] tee config rc=%d stderr=%r", result.returncode, result.stderr)
        if result.returncode != 0:
            return False, result.stderr or "Failed to write nginx config"

        # Symlink into sites-enabled
        if not os.path.exists(enabled_path):
            r = subprocess.run(
                ["sudo", "ln", "-sf", config_path, enabled_path],
                capture_output=True, text=True,
            )
            log.log(logging.WARNING if r.returncode else logging.DEBUG, "[nginx-cfg] symlink rc=%d stderr=%r", r.returncode, r.stderr)
            if r.returncode != 0:
                return False, r.stderr or "Failed to enable nginx site"
        else:
            log.debug("[nginx-cfg] symlink already exists at %s", enabled_path)
            # Always re-create symlink to ensure it points to current config
            subprocess.run(["sudo", "ln", "-sf", config_path, enabled_path], capture_output=True)

        # Validate config — if a *different* app's broken config causes the failure,
        # disable it automatically and retry once.
        result = subprocess.run(["sudo", "nginx", "-t"], capture_output=True, text=True)
        log.log(logging.WARNING if result.returncode else logging.DEBUG, "[nginx-cfg] nginx -t rc=%d stdout=%r stderr=%r", result.returncode, result.stdout, result.stderr)
        if result.returncode != 0:
            disabled = _disable_broken_configs(safe, result.stderr)
            if disabled:
                result = subprocess.run(["sudo", "nginx", "-t"], capture_output=True, text=True)
                log.log(logging.WARNING if result.returncode else logging.DEBUG, "[nginx-cfg] nginx -t retry rc=%d stderr=%r", result.returncode, result.stderr)
            if result.returncode != 0:
                return False, result.stderr

        r = subprocess.run(["sudo", "systemctl", "reload", "nginx"], capture_output=True, text=True)
        log.log(logging.WARNING if r.returncode else logging.DEBUG, "[nginx-cfg] reload rc=%d stderr=%r", r.returncode, r.stderr)
        return True, "OK"
    except FileNotFoundError:
        log.error("[nginx-cfg] nginx not found")
        return False, "nginx not found  -  install nginx first (sudo apt install nginx)"
    except Exception as e:
        log.exception("[nginx-cfg] unexpected error")
        return False, str(e)


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
    remove_nginx_config("cloudbase-acme")  # name used by earlier versions


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
