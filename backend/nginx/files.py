import functools
import logging
import os
import subprocess
import hashlib
import threading
from nginx.config import _build_default_catch_all_config
from nginx.paths import ACME_CONFIG_NAME, ACME_WEBROOT, MAINTENANCE_DIR, NGINX_ENABLED_DIR, NGINX_SITES_DIR, _ACME_LOCATION, _normalize_domain, _safe_name

log = logging.getLogger("cloudbase.nginx")

# Writing a config means: write, `nginx -t`, reload. Two of those at once would test each other's half-written
# files, so they take turns. They run in threads (asyncio.to_thread), never on the event loop: a reload can
# take seconds and would hold up every other request.
_LOCK = threading.RLock()


# Configs that were written but that nginx did not accept: these are never skipped as "unchanged"
_failed: set[str] = set()

# While a batch is open, writes and removals only touch files. One `nginx -t` and one reload follow when it
# ends, instead of one pair per app (each of which parses every config on the server).
_batch_depth = 0
_batch_pending: set[str] = set()


def _same_on_disk(path: str, content: str) -> bool:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read() == content
    except OSError:
        return False


def _test_and_reload(current_safe: str) -> tuple[bool, str]:
    """`nginx -t`, then reload. A different app's broken config (missing certificate) is switched off and tested once more."""
    result = subprocess.run(["sudo", "nginx", "-t"], capture_output=True, text=True)
    log.log(logging.WARNING if result.returncode else logging.DEBUG, "[nginx-cfg] nginx -t rc=%d stdout=%r stderr=%r", result.returncode, result.stdout, result.stderr)
    if result.returncode != 0:
        disabled = _disable_broken_configs(current_safe, result.stderr)
        if disabled:
            result = subprocess.run(["sudo", "nginx", "-t"], capture_output=True, text=True)
            log.log(logging.WARNING if result.returncode else logging.DEBUG, "[nginx-cfg] nginx -t retry rc=%d stderr=%r", result.returncode, result.stderr)
        if result.returncode != 0:
            return False, result.stderr
    r = subprocess.run(["sudo", "systemctl", "reload", "nginx"], capture_output=True, text=True)
    log.log(logging.WARNING if r.returncode else logging.DEBUG, "[nginx-cfg] reload rc=%d stderr=%r", r.returncode, r.stderr)
    return True, "OK"


def _serialized(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _LOCK:
            return fn(*args, **kwargs)
    return wrapper


@_serialized
def begin_batch() -> None:
    """From here on writes only touch files; `end_batch()` tests and reloads once."""
    global _batch_depth
    _batch_depth += 1


@_serialized
def end_batch() -> tuple[bool, str]:
    global _batch_depth
    _batch_depth = max(0, _batch_depth - 1)
    if _batch_depth > 0 or not _batch_pending:
        return True, "OK"
    pending = set(_batch_pending)
    _batch_pending.clear()
    ok, msg = _test_and_reload("")
    if ok:
        _failed.difference_update(pending)
    else:
        _failed.update(pending)
    return ok, msg


@_serialized
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


@_serialized
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


@_serialized
def write_default_catch_all(ssl_cert: str | None = None, ssl_key: str | None = None) -> tuple[bool, str]:
  """Write a default_server block for unmatched requests with a branded 404 page.
  Includes HTTPS default_server when a valid cert/key pair is provided."""
  config_path = os.path.join(NGINX_SITES_DIR, "cloudbase-default")
  enabled_path = os.path.join(NGINX_ENABLED_DIR, "cloudbase-default")
  try:
    catch_all_cfg = _build_default_catch_all_config(ssl_cert, ssl_key)
    if "cloudbase-default" not in _failed and os.path.exists(enabled_path) and _same_on_disk(config_path, catch_all_cfg):
      return True, "unchanged"
    r = subprocess.run(["sudo", "tee", config_path], input=catch_all_cfg, text=True, capture_output=True)
    if r.returncode != 0:
      return False, r.stderr or "Failed to write default catch-all"
    subprocess.run(["sudo", "ln", "-sf", config_path, enabled_path], capture_output=True)
    if _batch_depth > 0:
      _batch_pending.add("cloudbase-default")
      return True, "queued"
    ok, msg = _test_and_reload("cloudbase-default")
    (_failed.discard if ok else _failed.add)("cloudbase-default")
    return ok, msg
  except Exception as e:
    return False, str(e)


@_serialized
def write_maintenance_files(app_id: int, downtime_html: str, update_html: str, restart_html: str = None, starting_html: str = None) -> tuple[bool, str]:
    """Write downtime.html, update.html (and optionally restart.html, starting.html) to /var/www/cloudbase/maintenance/{app_id}/."""
    app_dir = os.path.join(MAINTENANCE_DIR, str(app_id))
    log.debug("[maint-files] writing to %s", app_dir)
    try:
        files = [("downtime.html", downtime_html), ("update.html", update_html)]
        if restart_html is not None:
            files.append(("restart.html", restart_html))
        if starting_html is not None:
            files.append(("starting.html", starting_html))
        # Pages that are already there as they should be need no write: most saves change none of them
        files = [(f, c) for f, c in files if not _same_on_disk(os.path.join(app_dir, f), c)]
        if not files:
            return True, "unchanged"

        r = subprocess.run(["sudo", "mkdir", "-p", app_dir], capture_output=True, text=True)
        log.log(logging.WARNING if r.returncode else logging.DEBUG, "[maint-files] mkdir rc=%d stderr=%r", r.returncode, r.stderr)
        if r.returncode != 0:
            return False, r.stderr or "Failed to create maintenance directory"

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


@_serialized
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
        # The same config, already live: nothing to write, test or reload
        if safe not in _failed and os.path.exists(enabled_path) and _same_on_disk(config_path, cfg):
            log.debug("[nginx-cfg] config for %r is unchanged", safe)
            return True, "unchanged"

        # Write via sudo tee (works without direct write permission)
        result = subprocess.run(
            ["sudo", "tee", config_path],
            input=config, text=True, capture_output=True,
        )
        log.log(logging.WARNING if result.returncode else logging.DEBUG, "[nginx-cfg] tee config rc=%d stderr=%r", result.returncode, result.stderr)
        if result.returncode != 0:
            _failed.add(safe)
            return False, result.stderr or "Failed to write nginx config"

        # Symlink into sites-enabled
        if not os.path.exists(enabled_path):
            r = subprocess.run(
                ["sudo", "ln", "-sf", config_path, enabled_path],
                capture_output=True, text=True,
            )
            log.log(logging.WARNING if r.returncode else logging.DEBUG, "[nginx-cfg] symlink rc=%d stderr=%r", r.returncode, r.stderr)
            if r.returncode != 0:
                _failed.add(safe)
                return False, r.stderr or "Failed to enable nginx site"
        else:
            log.debug("[nginx-cfg] symlink already exists at %s", enabled_path)
            # Always re-create symlink to ensure it points to current config
            subprocess.run(["sudo", "ln", "-sf", config_path, enabled_path], capture_output=True)

        if _batch_depth > 0:
            _batch_pending.add(safe)
            return True, "queued"

        # Validate the config (a different app's broken one is switched off), then reload
        ok, msg = _test_and_reload(safe)
        (_failed.discard if ok else _failed.add)(safe)
        return ok, msg
    except FileNotFoundError:
        log.error("[nginx-cfg] nginx not found")
        return False, "nginx not found  -  install nginx first (sudo apt install nginx)"
    except Exception as e:
        log.exception("[nginx-cfg] unexpected error")
        return False, str(e)


@_serialized
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


@_serialized
def remove_acme_server() -> None:
    remove_nginx_config(ACME_CONFIG_NAME)
    remove_nginx_config("cloudbase-acme")  # name used by earlier versions


@_serialized
def write_acme_probe(token: str, content: str) -> tuple[bool, str]:
    """Place a file in the challenge webroot (owned by the Cloudbase user)."""
    path = os.path.join(ACME_WEBROOT, ".well-known", "acme-challenge", token)
    try:
        with open(path, "w", encoding="ascii") as f:
            f.write(content)
        return True, path
    except OSError as e:
        return False, str(e)


@_serialized
def remove_acme_probe(token: str) -> None:
    try:
        os.remove(os.path.join(ACME_WEBROOT, ".well-known", "acme-challenge", token))
    except OSError:
        pass


@_serialized
def remove_nginx_config(app_name: str) -> bool:
    safe = _safe_name(app_name)
    config_path = os.path.join(NGINX_SITES_DIR, safe)
    enabled_path = os.path.join(NGINX_ENABLED_DIR, safe)

    if not any(os.path.lexists(p) for p in (enabled_path, config_path)):
        return False                                  # nothing there: no reload either

    removed = False
    for path in [enabled_path, config_path]:
        r = subprocess.run(["sudo", "rm", "-f", path], capture_output=True)
        if r.returncode == 0:
            removed = True
    _failed.discard(safe)

    if removed:
        if _batch_depth > 0:
            _batch_pending.add(safe)
        else:
            subprocess.run(["sudo", "systemctl", "reload", "nginx"], capture_output=True)
    return removed
