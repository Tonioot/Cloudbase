"""Export and import of apps with their secrets, as one passphrase-encrypted file."""
import asyncio
import ipaddress
import json
import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core import auth as _auth
from core.audit import log_audit
from core.database import get_db
from core.models import User
from transfer import crypto, importer, manifest

router = APIRouter(prefix="/api/transfer", tags=["transfer"])

log = logging.getLogger("cloudbase.transfer")

MAX_IMPORT_BYTES = 64 * 1024 * 1024


class ExportRequest(BaseModel):
    app_ids: Optional[list[int]] = None   # None or empty: every app
    passphrase: str
    include_secrets: bool = False         # environment variables and Git tokens; needs secrets.export
    password: str = ""                    # the account password again; required when secrets are included


def _client_ip(request: Request) -> str:
    host = request.client.host if request.client else ""
    if host in ("127.0.0.1", "::1"):      # behind the local nginx: use the visitor's address
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
        raise HTTPException(403, "This connection is not encrypted. Open the panel over https before exporting or importing secrets.")


async def _confirm_password(db: AsyncSession, username: str, password: str, request: Request) -> None:
    _auth._check_rate_limit(_client_ip(request))    # the same limit as logging in
    user = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if not user or not _auth.verify_password(password or "", user.password_hash):
        raise HTTPException(403, "Wrong password")


@router.post("/export")
async def export_bundle(
    req: ExportRequest,
    request: Request,
    user: dict = Depends(_auth.require_permission("apps.configure")),
    actor: str = Depends(_auth.get_current_actor),
    db: AsyncSession = Depends(get_db),
):
    _require_private_transport(request)
    if req.include_secrets:
        # Secrets are write-only everywhere else in Cloudbase (the API never returns them), so reading
        # them back out through an export needs its own permission, not just the right to edit apps.
        if user.get("permissions") is not None and "secrets.export" not in user["permissions"]:
            raise HTTPException(403, "Permission 'secrets.export' required to export environment variables and Git tokens")
        await _confirm_password(db, user["username"], req.password, request)
    try:
        crypto.check_passphrase(req.passphrase)
    except crypto.TransferCryptoError as exc:
        raise HTTPException(400, str(exc))

    content = await manifest.build_manifest(db, req.app_ids, req.include_secrets)
    blob = await asyncio.to_thread(crypto.encrypt_bytes, json.dumps(content).encode("utf-8"), req.passphrase)
    await log_audit(db, "transfer.export", actor=actor, detail={"apps": len(content["apps"]), "secrets": req.include_secrets})   # never the content
    await db.commit()
    return Response(
        content=blob,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="cloudbase-export-{date.today().isoformat()}.cloudbase"',
            "Cache-Control": "no-store",
            "X-App-Count": str(len(content["apps"])),
        },
    )


@router.post("/import")
async def import_bundle(
    background_tasks: BackgroundTasks,
    request: Request,
    file: UploadFile = File(...),
    passphrase: str = Form(...),
    target_node_id: Optional[int] = Form(None),
    _user: dict = Depends(_auth.require_permission("apps.deploy")),
    actor: str = Depends(_auth.get_current_actor),
    db: AsyncSession = Depends(get_db),
):
    _require_private_transport(request)
    _auth._check_rate_limit(_client_ip(request))    # a passphrase can be guessed here
    blob = await file.read(MAX_IMPORT_BYTES + 1)
    if len(blob) > MAX_IMPORT_BYTES:
        raise HTTPException(413, "This file is too large to import")
    try:
        data = await asyncio.to_thread(crypto.decrypt_bytes, blob, passphrase)
        entries = manifest.check_manifest(json.loads(data.decode("utf-8")))
    except (crypto.TransferCryptoError, ValueError) as exc:
        raise HTTPException(400, str(exc))

    result = await importer.import_apps(db, entries, target_node_id, actor)
    await log_audit(db, "transfer.import", actor=actor, detail={"imported": len(result.imported), "skipped": len(result.skipped)})
    await db.commit()
    if result.imported:
        background_tasks.add_task(importer.deploy_imported, result.imported, result.target_is_local, actor)
    return {
        "imported": [{"name": i.name, "instances": i.instances, "will_start": i.run} for i in result.imported],
        "skipped": result.skipped,
        "warnings": result.warnings,
    }
