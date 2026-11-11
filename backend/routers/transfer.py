"""Export and import of apps and databases as one file.

Exporting is a background job (a database dump can take minutes): start it, poll it, download it.
Who may do what follows the permissions the rest of Cloudbase already has:
  apps            apps.configure (export), apps.deploy (import)
  databases       databases.view (export), databases.create / manage (import)
  database data   databases.backup, the same right as downloading or restoring a backup
  secrets         secrets.export: env vars and Git tokens are write-only everywhere else
"""
import asyncio
import ipaddress
import logging
import os
import shutil
import tempfile
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask
from starlette.background import BackgroundTasks

from core import auth as _auth
from core.audit import log_audit
from core.database import get_db
from core.models import Application, Database, User
from transfer import archive, db_import, exporter, full, importer, jobs, manifest, settings

router = APIRouter(prefix="/api/transfer", tags=["transfer"])

log = logging.getLogger("cloudbase.transfer")

MAX_IMPORT_BYTES = 8 * 1024 ** 3


class ExportRequest(BaseModel):
    app_ids: Optional[list[int]] = None        # None: every app. A list, even an empty one: exactly those
    database_ids: Optional[list[int]] = None   # None or empty: no databases
    include_data: bool = False                 # dumps of the selected databases; needs databases.backup
    include_secrets: bool = False              # environment variables and Git tokens; needs secrets.export
    include_settings: bool = False             # users, roles, channels, tokens, storage, System settings; needs secrets.export
    password: str = ""                         # the account password again; required when secrets are included


def _client_ip(request: Request) -> str:
    host = request.client.host if request.client else ""
    if host in ("127.0.0.1", "::1"):           # behind the local nginx: use the visitor's address
        return request.headers.get("x-real-ip") or host
    return host


def _require_private_transport(request: Request) -> None:
    """The file (which may hold secrets) travels over this connection. Over plain HTTP that is only
    acceptable on a private network; from a public address it is refused."""
    if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
        return
    try:
        ip = ipaddress.ip_address(_client_ip(request))
    except ValueError:
        return
    if ip.is_global:      # LAN, loopback, link-local and VPN ranges (Tailscale) are not global
        raise HTTPException(403, "This connection is not encrypted. Open the panel over https before exporting or importing.")


def _need(user: dict, permission: str, why: str) -> None:
    perms = user.get("permissions")            # None: the root account
    if perms is not None and permission not in perms:
        raise HTTPException(403, f"Permission '{permission}' required to {why}")


async def _confirm_password(db: AsyncSession, username: str, password: str, request: Request) -> None:
    _auth._check_rate_limit(_client_ip(request))    # the same limit as logging in
    user = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if not user or not _auth.verify_password(password or "", user.password_hash):
        raise HTTPException(403, "Wrong password")


@router.post("/export")
async def start_export(
    req: ExportRequest,
    request: Request,
    user: dict = Depends(_auth.require_permission("apps.view")),
    actor: str = Depends(_auth.get_current_actor),
    db: AsyncSession = Depends(get_db),
):
    _require_private_transport(request)
    if req.app_ids is None or req.app_ids:
        _need(user, "apps.configure", "export apps")
    if req.database_ids:
        _need(user, "databases.view", "export databases")
    if req.include_data:
        _need(user, "databases.backup", "include database data")
    if req.include_secrets or req.include_settings:
        # Secrets are write-only everywhere else in Cloudbase (the API never returns them), so reading
        # them back out through an export needs its own permission, not just the right to edit apps.
        _need(user, "secrets.export", "export environment variables, Git tokens and the settings and users")
        await _confirm_password(db, user["username"], req.password, request)
    job = jobs.create(user["username"])
    asyncio.create_task(exporter.run_export(
        job, app_ids=req.app_ids, database_ids=req.database_ids, include_data=req.include_data,
        include_secrets=req.include_secrets, include_settings=req.include_settings, actor=actor))
    return {"job_id": job.id}


@router.get("/export/{job_id}")
async def export_status(job_id: str, user: dict = Depends(_auth.require_permission("apps.view"))):
    job = jobs.get(job_id, user["username"])
    if not job:
        raise HTTPException(404, "Export not found (it may have expired)")
    return job.public()


@router.get("/export/{job_id}/download")
async def download_export(job_id: str, user: dict = Depends(_auth.require_permission("apps.view"))):
    job = jobs.get(job_id, user["username"])
    if not job or job.state != "done" or not os.path.exists(job.path):
        raise HTTPException(404, "This export is not ready, or it was already downloaded")
    return FileResponse(
        job.path,
        media_type="application/octet-stream",
        filename=(f"cloudbase-backup-{date.today().isoformat()}.tar.gz" if job.kind == "full"
                  else f"cloudbase-export-{date.today().isoformat()}.cloudbase"),
        headers={"Cache-Control": "no-store", "X-App-Count": str(job.apps), "X-Database-Count": str(job.databases)},
        background=BackgroundTask(jobs.remove, job),      # one download, then it is gone
    )


async def _receive(file: UploadFile, dest: str) -> None:
    size = 0
    with open(dest, "wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_IMPORT_BYTES:
                raise HTTPException(413, "This file is too large to import")
            out.write(chunk)


class PreviewRequest(BaseModel):
    manifest: dict                             # the file's manifest.json, read by the page


@router.post("/preview")
async def preview_import(
    req: PreviewRequest,
    user: dict = Depends(_auth.require_permission("apps.view")),
    db: AsyncSession = Depends(get_db),
):
    """What an import of this file would do, before it is done: the same rules as the import itself."""
    try:
        app_entries = manifest.check_manifest(req.manifest)
        database_entries = manifest.databases_of(req.manifest)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    perms = user.get("permissions")
    may = lambda p: perms is None or p in perms  # noqa: E731
    existing_apps = set((await db.execute(select(Application.name))).scalars())
    existing_dbs = set((await db.execute(select(Database.name))).scalars())

    def plan(name, exists, permission):
        if exists:
            return "skip", "already exists"
        if not may(permission):
            return "skip", f"needs the '{permission}' permission"
        return "add", ""

    apps = []
    for e in app_entries:
        name = str(e.get("name") or "").strip()
        action, reason = plan(name, name in existing_apps, "apps.deploy")
        apps.append({"name": name, "instances": int(e.get("instances") or 0), "start": bool(e.get("run")),
                     "action": action, "reason": reason})
    databases = []
    for e in database_entries:
        name = str(e.get("name") or "").strip()
        action, reason = plan(name, name in existing_dbs, "databases.create")
        data = bool(e.get("data"))
        databases.append({"name": name, "engine": e.get("engine"), "version": e.get("version"), "action": action,
                          "reason": reason, "data": data and may("databases.backup"),
                          "links": [link.get("app") for link in (e.get("links") or []) if isinstance(link, dict)]})
    settings_plan = None
    if "settings" in req.manifest:
        settings_plan = ({"allowed": False} if perms is not None
                         else {"allowed": True, "items": await settings.preview_settings(db, req.manifest["settings"])})
    return {"created_at": req.manifest.get("created_at"), "secrets": bool(req.manifest.get("secrets_included")),
            "apps": apps, "databases": databases, "settings": settings_plan}


@router.post("/import")
async def import_bundle(
    background_tasks: BackgroundTasks,
    request: Request,
    file: UploadFile = File(...),
    target_node_id: Optional[int] = Form(None),
    user: dict = Depends(_auth.require_permission("apps.view")),
    actor: str = Depends(_auth.get_current_actor),
    db: AsyncSession = Depends(get_db),
):
    _require_private_transport(request)
    perms = user.get("permissions")

    jobs._ensure_dir()
    work = tempfile.mkdtemp(prefix="import-", dir=jobs.EXPORT_DIR)
    tar = None
    try:
        upload = os.path.join(work, "upload")
        await _receive(file, upload)
        try:
            tar = archive.open_archive(upload)
            content = archive.read_manifest(tar)
            entries = manifest.check_manifest(content)
            database_entries = manifest.databases_of(content)
        except (archive.ArchiveError, ValueError) as exc:
            raise HTTPException(400, str(exc))

        apps = await importer.import_apps(db, entries, target_node_id, actor, perms)
        app_ids = {i.name: i.id for i in apps.imported}
        imported_dbs, skipped_dbs = await db_import.import_databases(
            db, tar, database_entries, app_ids, target_node_id, perms, actor, apps.warnings)
        settings_result = None
        if "settings" in content:
            if perms is not None:              # it can add accounts and roles: the root account only
                apps.warnings.append("The settings and users in this file were skipped: only the admin account can import them")
            else:
                try:
                    settings_result = await settings.import_settings(db, content["settings"])
                except ValueError as exc:
                    raise HTTPException(400, str(exc))
        await log_audit(db, "transfer.import", actor=actor, detail={
            "apps": len(apps.imported), "databases": len(imported_dbs),
            "skipped": len(apps.skipped) + len(skipped_dbs), "settings": settings_result is not None})
        await db.commit()
    finally:
        if tar is not None:
            tar.close()
        shutil.rmtree(work, ignore_errors=True)           # the uploaded file may hold secrets: do not keep it

    if apps.imported:
        background_tasks.add_task(importer.deploy_imported, apps.imported, apps.target_is_local, actor)
    return {
        "imported": [{"name": i.name, "instances": i.instances, "will_start": i.run} for i in apps.imported],
        "skipped": apps.skipped,
        "databases": [{"name": d.name, "engine": d.engine, "version": d.version, "links": d.links,
                       "restoring": d.restoring} for d in imported_dbs],
        "skipped_databases": skipped_dbs,
        "warnings": apps.warnings,
        "settings": settings_result,
    }


# ── Everything: a full backup of this installation, and putting one back ─────

class FullBackupRequest(BaseModel):
    include_data: bool = False                 # a fresh dump of every running database
    password: str = ""


def _root_only(user: dict, why: str) -> None:
    if user.get("permissions") is not None:
        raise HTTPException(403, f"Only the admin account can {why}")


@router.post("/full-backup")
async def start_full_backup(
    req: FullBackupRequest,
    request: Request,
    user: dict = Depends(_auth.require_permission("apps.view")),
    actor: str = Depends(_auth.get_current_actor),
    db: AsyncSession = Depends(get_db),
):
    """Everything this installation needs to come back, keys included: the admin account only, with its password."""
    _require_private_transport(request)
    _root_only(user, "make a full backup")
    await _confirm_password(db, user["username"], req.password, request)
    job = jobs.create(user["username"], kind="full")
    asyncio.create_task(full.run_full_backup(job, req.include_data, actor))
    return {"job_id": job.id}


@router.post("/restore")
async def restore_full_backup(
    request: Request,
    file: UploadFile = File(...),
    password: str = Form(""),
    user: dict = Depends(_auth.require_permission("apps.view")),
    actor: str = Depends(_auth.get_current_actor),
    db: AsyncSession = Depends(get_db),
):
    """Check a full backup, put it aside and restart: the next start puts it in place (transfer/full.py)."""
    _require_private_transport(request)
    _root_only(user, "restore a full backup")
    await _confirm_password(db, user["username"], password, request)
    jobs._ensure_dir()
    work = tempfile.mkdtemp(prefix="restore-", dir=jobs.EXPORT_DIR)
    try:
        upload = os.path.join(work, "upload")
        await _receive(file, upload)
        if not full.is_full_backup(upload):
            raise HTTPException(400, "This is an export file, not a full backup: import it with one of the other options")
        try:
            info = await asyncio.to_thread(full.check, upload)
        except full.RestoreError as exc:
            raise HTTPException(400, str(exc))
        await log_audit(db, "transfer.restore", actor=actor, detail={k: info.get(k) for k in ("version", "created", "host")})
        await db.commit()
        full.stage(upload)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    full.leave_for_restart()
    return {**info, "restarting": True}
