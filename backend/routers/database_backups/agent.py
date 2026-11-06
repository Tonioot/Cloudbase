import os
from typing import Optional
from fastapi import Header, HTTPException, Depends, Request, APIRouter
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import get_db
from core.models import DatabaseBackup
from dbs.backups import _ensure_local, _node_for_database, _path, _restores

agent_router = APIRouter(prefix="/api/nodes/agent/databases", tags=["database-backups"])


@agent_router.post("/{database_id}/backups/{backup_id}/upload")
async def agent_upload_backup(
    database_id: int,
    backup_id: int,
    request: Request,
    x_node_token: Optional[str] = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    await _node_for_database(database_id, x_node_token, db)
    backup = await db.get(DatabaseBackup, backup_id)
    if backup is None or backup.database_id != database_id or backup.status != "running":
        raise HTTPException(409, "No running backup with this id")
    path = _path(backup)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    part = path + ".part"
    with open(part, "wb") as out:
        async for chunk in request.stream():
            out.write(chunk)
    os.replace(part, path)
    os.chmod(path, 0o600)
    return {"ok": True, "size_bytes": os.path.getsize(path)}


@agent_router.get("/{database_id}/backups/{backup_id}/download")
async def agent_download_backup(
    database_id: int,
    backup_id: int,
    x_node_token: Optional[str] = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    await _node_for_database(database_id, x_node_token, db)
    restore = _restores.get(database_id) or {}
    if restore.get("state") != "running" or restore.get("backup_id") != backup_id:
        raise HTTPException(403, "No restore of this backup is in progress for this database")
    backup = await db.get(DatabaseBackup, backup_id)
    if backup is None or not await _ensure_local(backup):
        raise HTTPException(404, "Backup file not available")
    return FileResponse(_path(backup), media_type="application/octet-stream")
