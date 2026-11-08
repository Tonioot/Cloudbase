import os
from fastapi import HTTPException, File, UploadFile, Depends, APIRouter
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import get_db
from containers import naming as containers_naming, processes as pm
from core import auth as _auth
from apps.serializers import _get_or_404

router = APIRouter(prefix="/api/apps", tags=["applications"])


@router.get("/system/certs")
async def discover_certs():
    """Scan common certificate and key locations on this machine."""
    import glob

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


@router.get("/{app_id}/certs")
async def discover_app_certs(app_id: int, db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("apps.view"))):
    """Scan for cert/key files inside the app's working directory only."""
    app = await _get_or_404(app_id, db)

    base = pm.app_source_dir(app)
    if not base or not os.path.isdir(base):
        return {"certs": [], "keys": []}

    _SKIP_DIRS = {"venv", ".venv", "node_modules", "site-packages", "certifi", ".git", "__pycache__", ".tox", ".mypy_cache"}
    _CERT_EXTS = {".pem", ".crt", ".cer"}
    _KEY_EXTS  = {".pem", ".key"}
    _CA_BUNDLE_NAMES = {"cacert.pem", "ca-bundle.crt", "ca-bundle.pem", "ca-certificates.crt"}

    certs: list[str] = []
    keys:  list[str] = []

    for dirpath, dirnames, filenames in os.walk(base):
        # Prune ignored directories in-place so os.walk skips them entirely
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            if fname in _CA_BUNDLE_NAMES:
                continue
            ext = os.path.splitext(fname)[1].lower()
            fpath = os.path.join(dirpath, fname)
            if ext in _CERT_EXTS:
                certs.append(fpath)
            if ext in _KEY_EXTS:
                keys.append(fpath)

    # Heuristic: files with 'key' in name are more likely private keys
    key_set  = sorted({p for p in set(keys)  if "key" in os.path.basename(p).lower() or p.endswith(".key")})
    cert_set = sorted({p for p in set(certs) if "key" not in os.path.basename(p).lower()})
    # Fallback: if no dedicated key files found, show all .pem
    if not key_set:
        key_set = sorted(set(keys))

    return {"certs": cert_set, "keys": key_set}


@router.post("/{app_id}/certs/upload")
async def upload_app_cert(app_id: int, file: UploadFile = File(...), _user: dict = Depends(_auth.require_permission("apps.configure")), db: AsyncSession = Depends(get_db)):
    """Upload a cert/key file into the app's certs subfolder and return its path."""
    app = await _get_or_404(app_id, db)

    allowed_exts = {".pem", ".crt", ".cer", ".key"}
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in allowed_exts:
        raise HTTPException(400, "Only .pem, .crt, .cer, .key files are allowed")
    safe_name = os.path.basename(file.filename).replace("..", "").lstrip("/")
    if not safe_name or safe_name.startswith("."):
        raise HTTPException(400, "Invalid file name")
    base = pm.app_source_dir(app) if app.working_dir else os.path.expanduser(f"~/.cloudbase/certs/{containers_naming.app_dir_name(app.id)}")
    if not base:
        raise HTTPException(400, "App source directory is outside the Cloudbase apps folder")
    dest_dir = os.path.join(base, "certs")
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, safe_name)
    contents = await file.read()
    with open(dest_path, "wb") as f:
        f.write(contents)
    os.chmod(dest_path, 0o600)
    return {"path": dest_path}
