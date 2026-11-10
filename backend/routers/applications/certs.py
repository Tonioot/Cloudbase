import glob
import os
from fastapi import APIRouter, Depends
from core import auth as _auth

router = APIRouter(prefix="/api/apps", tags=["applications"])


@router.get("/system/certs")
async def discover_certs(_user: dict = Depends(_auth.require_permission("apps.view"))):
    """Scan common certificate and key locations on this machine (for picking a certificate for an app's domain)."""

    cert_patterns = [
        "/etc/letsencrypt/live/*/fullchain.pem",
        "/etc/letsencrypt/live/*/cert.pem",
        "/etc/ssl/certs/*.pem",
        "/etc/ssl/certs/*.crt",
        "/etc/nginx/ssl/*.pem",
        "/etc/nginx/ssl/*.crt",
        "/etc/nginx/certs/*.pem",
        "/etc/nginx/certs/*.crt",
        os.path.expanduser("~/.cloudbase/certs/*.pem"),
        os.path.expanduser("~/.cloudbase/certs/*.crt"),
    ]
    key_patterns = [
        "/etc/letsencrypt/live/*/privkey.pem",
        "/etc/ssl/private/*.pem",
        "/etc/ssl/private/*.key",
        "/etc/nginx/ssl/*.key",
        "/etc/nginx/certs/*.key",
        os.path.expanduser("~/.cloudbase/certs/*.key"),
        os.path.expanduser("~/.cloudbase/certs/*.pem"),
    ]

    certs: list[str] = []
    keys: list[str] = []

    for pattern in cert_patterns:
        try:
            certs.extend(glob.glob(pattern))
        except Exception:
            pass
    for pattern in key_patterns:
        try:
            keys.extend(glob.glob(pattern))
        except Exception:
            pass

    return {"certs": sorted(set(certs)), "keys": sorted(set(keys))}
