import json
from fastapi import Depends, APIRouter
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import get_db
from nginx import paths as nginx_paths
from nginx import config as nginx_config
from core import auth as _auth
from apps.nginx_sync import _get_nginx_backends, _get_nginx_mode
from apps.serializers import _get_or_404

router = APIRouter(prefix="/api/apps", tags=["applications"])


@router.get("/{app_id}/nginx-debug")
async def nginx_debug(app_id: int, db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("apps.configure"))):
    """Return a full diagnostic snapshot for nginx + maintenance config of this app."""
    import subprocess as sp
    app = await _get_or_404(app_id, db)

    safe_name = nginx_paths.app_key(app.id)
    config_path   = f"{nginx_paths.NGINX_SITES_DIR}/{safe_name}"
    enabled_path  = f"{nginx_paths.NGINX_ENABLED_DIR}/{safe_name}"
    maint_dir     = f"{nginx_paths.MAINTENANCE_DIR}/{app_id}"

    def _read_file(path: str) -> str:
        r = sp.run(["sudo", "cat", path], capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout
        return f"ERROR ({r.returncode}): {r.stderr.strip()}"

    def _ls(path: str) -> list:
        r = sp.run(["sudo", "ls", "-la", path], capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout.strip().splitlines()
        return [f"ERROR: {r.stderr.strip()}"]

    nginx_test  = sp.run(["sudo", "nginx", "-t"], capture_output=True, text=True)
    nginx_status = sp.run(["sudo", "systemctl", "is-active", "nginx"], capture_output=True, text=True)

    return {
        "app": {
            "id":               app.id,
            "name":             app.name,
            "domain":           app.domain,
            "port":             app.port,
            "nginx_enabled":    app.nginx_enabled,
            "maintenance_mode": app.maintenance_mode,
            "update_mode":      app.update_mode,
            "computed_mode":    _get_nginx_mode(app),
        },
        "nginx": {
            "status":           nginx_status.stdout.strip(),
            "config_test":      nginx_test.stderr.strip() or nginx_test.stdout.strip(),
            "config_test_ok":   nginx_test.returncode == 0,
        },
        "files": {
            "sites_available_exists": sp.run(["sudo", "test", "-f", config_path], capture_output=True).returncode == 0,
            "sites_enabled_exists":   sp.run(["sudo", "test", "-L", enabled_path], capture_output=True).returncode == 0,
            "maintenance_dir_ls":     _ls(maint_dir),
            "nginx_config_content":   _read_file(config_path),
        },
        "conflicts": {
            "description": "Other enabled nginx configs that also define this domain (should be empty)",
            "files": [
                line for line in
                sp.run(
                    ["sudo", "grep", "-rl", app.domain or "", nginx_paths.NGINX_ENABLED_DIR],
                    capture_output=True, text=True,
                ).stdout.strip().splitlines()
                if line and not line.endswith("/" + safe_name)
            ] if app.domain else [],
        },
        "generated_config": nginx_config.generate_config(
            app.name, app.domain or "(no domain)", await _get_nginx_backends(app, db),
            app.ssl_cert_path, app.ssl_key_path,
            app_id=app_id, mode=_get_nginx_mode(app),
            extra_domains=json.loads(app.extra_domains or "[]"),
            redirect_domains=json.loads(app.redirect_domains or "[]"),
        ),
    }
