"""Export and import of apps and databases as one passphrase-encrypted file.

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
from core.models import User
from transfer import archive, crypto, db_import, exporter, importer, jobs, manifest

router = APIRouter(prefix="/api/transfer", tags=["transfer"])

log = logging.getLogger("cloudbase.transfer")

MAX_IMPORT_BYTES = 8 * 1024 ** 3


class ExportRequest(BaseModel):
    app_ids: Optional[list[int]] = None        # None: every app. A list, even an empty one: exactly those
    database_ids: Optional[list[int]] = None   # None or empty: no databases
    include_data: bool = False                 # dumps of the selected databases; needs databases.backup
    include_secrets: bool = False              # environment variables and Git tokens; needs secrets.export
    passphrase: str
    password: str = ""                         # the account password again; required when secrets are included


def _client_ip(request: Request) -> str:
    host = request.client.host if request.client else ""
    if host in ("127.0.0.1", "::1"):           # behind the local nginx: use the visitor's address
        return request.headers.get("x-real-ip") or host
    return host


def _require_private_transport(request: Request) -> None:
    """The passphrase and the file travel over this connection. Over plain HTTP that is only
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
    if req.include_secrets:
        # Secrets are write-only everywhere else in Cloudbase (the API never returns them), so reading
        # them back out through an export needs its own permission, not just the right to edit apps.
        _need(user, "secrets.export", "export environment variables and Git tokens")
        await _confirm_password(db, user["username"], req.password, request)
    try:
        crypto.check_passphrase(req.passphrase)
    except crypto.TransferCryptoError as exc:
        raise HTTPException(400, str(exc))

    job = jobs.create(user["username"])
    asyncio.create_task(exporter.run_export(
        job, app_ids=req.app_ids, database_ids=req.database_ids, include_data=req.include_data,
        include_secrets=req.include_secrets, passphrase=req.passphrase, actor=actor))
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
        filename=f"cloudbase-export-{date.today().isoformat()}.cloudbase",
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


@router.post("/import")
async def import_bundle(
    background_tasks: BackgroundTasks,
    request: Request,
    file: UploadFile = File(...),
    passphrase: str = Form(...),
    target_node_id: Optional[int] = Form(None),
    user: dict = Depends(_auth.require_permission("apps.view")),
    actor: str = Depends(_auth.get_current_actor),
    db: AsyncSession = Depends(get_db),
):
    _require_private_transport(request)
    _auth._check_rate_limit(_client_ip(request))    # a passphrase can be guessed here
    perms = user.get("permissions")

    jobs._ensure_dir()
    work = tempfile.mkdtemp(prefix="import-", dir=jobs.EXPORT_DIR)
    tar = None
    try:
        upload, plain = os.path.join(work, "upload"), os.path.join(work, "plain.tar")
        await _receive(file, upload)
        try:
            await asyncio.to_thread(crypto.decrypt_file, upload, plain, passphrase)
            os.remove(upload)
            tar = archive.open_archive(plain)
            content = archive.read_manifest(tar)
            entries = manifest.check_manifest(content)
            database_entries = manifest.databases_of(content)
        except (crypto.TransferCryptoError, archive.ArchiveError, ValueError) as exc:
            raise HTTPException(400, str(exc))

        apps = await importer.import_apps(db, entries, target_node_id, actor, perms)
        app_ids = {i.name: i.id for i in apps.imported}
        imported_dbs, skipped_dbs = await db_import.import_databases(
            db, tar, database_entries, app_ids, target_node_id, perms, actor, apps.warnings)
        await log_audit(db, "transfer.import", actor=actor, detail={
            "apps": len(apps.imported), "databases": len(imported_dbs),
            "skipped": len(apps.skipped) + len(skipped_dbs)})
        await db.commit()
    finally:
        if tar is not None:
            tar.close()
        shutil.rmtree(work, ignore_errors=True)           # the decrypted archive must not stay behind

    if apps.imported:
        background_tasks.add_task(importer.deploy_imported, apps.imported, apps.target_is_local, actor)
    return {
        "imported": [{"name": i.name, "instances": i.instances, "will_start": i.run} for i in apps.imported],
        "skipped": apps.skipped,
        "databases": [{"name": d.name, "engine": d.engine, "version": d.version, "links": d.links,
                       "restoring": d.restoring} for d in imported_dbs],
        "skipped_databases": skipped_dbs,
        "warnings": apps.warnings,
    }
