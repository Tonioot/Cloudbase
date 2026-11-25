"""API tokens: keys for another program (a script, a backend built on Cloudbase) to call this API. A token has its
own list of permissions and optionally only some projects; it is not tied to a user or a role. Only the root account
manages them, and a token itself can never manage users, roles or tokens (those routes are the root account's)."""
import json
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core import auth
from core.audit import log_audit
from core.database import get_db
from core.models import ApiToken, Permission, Project

router = APIRouter(prefix="/api/tokens", tags=["api-tokens"])


class CreateTokenRequest(BaseModel):
    name: str
    permissions: list[str]
    project_ids: list[int] = []           # empty: every project
    expires_in_days: Optional[int] = None


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() + "Z" if dt else None


def _json(t: ApiToken) -> dict:
    return {"id": t.id, "name": t.name, "permissions": json.loads(t.permissions or "[]"),
            "project_ids": json.loads(t.project_ids) if t.project_ids else [], "prefix": t.prefix,
            "created_at": _iso(t.created_at), "expires_at": _iso(t.expires_at), "last_used_at": _iso(t.last_used_at)}


@router.get("")
async def list_tokens(_user: dict = Depends(auth.require_superadmin), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(ApiToken).order_by(ApiToken.created_at))).scalars()
    return [_json(t) for t in rows]


@router.post("")
async def create_token(req: CreateTokenRequest, admin_user: dict = Depends(auth.require_superadmin),
                       db: AsyncSession = Depends(get_db)):
    name = req.name.strip()
    if not name or len(name) > 60:
        raise HTTPException(status_code=400, detail="Give the token a name of 1 to 60 characters")
    permissions = sorted(set(req.permissions))
    if not permissions:
        raise HTTPException(status_code=400, detail="Give the token at least one permission")
    known = set((await db.execute(select(Permission.name))).scalars())
    if set(permissions) - known:
        raise HTTPException(status_code=400, detail=f"Unknown permission: {', '.join(sorted(set(permissions) - known))}")
    project_ids = sorted(set(req.project_ids))
    if project_ids:
        found = set((await db.execute(select(Project.id).where(Project.id.in_(project_ids)))).scalars())
        if found != set(project_ids):
            raise HTTPException(status_code=400, detail="Project not found")
    if req.expires_in_days is not None and req.expires_in_days < 1:
        raise HTTPException(status_code=400, detail="expires_in_days must be at least 1")
    token, token_hash, prefix = auth.new_api_token()
    row = ApiToken(name=name, permissions=json.dumps(permissions), project_ids=json.dumps(project_ids) if project_ids else None,
                   token_hash=token_hash, prefix=prefix,
                   expires_at=datetime.utcnow() + timedelta(days=req.expires_in_days) if req.expires_in_days else None)
    db.add(row)
    await log_audit(db, "token.create", actor=admin_user["username"],
                    detail={"name": name, "permissions": permissions, "projects": project_ids})
    await db.commit()
    await db.refresh(row)
    return {**_json(row), "token": token}      # the only time the token itself is shown


@router.post("/rotate")
async def rotate_own_token(request: Request, _user: dict = Depends(auth.require_auth), db: AsyncSession = Depends(get_db)):
    """A token replaces itself with a new value (same name, permissions and projects); the old value stops working at once.
    For a program that got its token in a way that may have left a copy behind, such as an install command."""
    old = auth.bearer_token(request.headers)
    row = (await db.execute(select(ApiToken).where(ApiToken.token_hash == auth.hash_api_token(old)))).scalar_one_or_none() if old else None
    if row is None:
        raise HTTPException(status_code=400, detail="Only an API token can replace itself (call this with the token)")
    token, token_hash, prefix = auth.new_api_token()
    row.token_hash, row.prefix = token_hash, prefix
    await log_audit(db, "token.rotate", actor=f"token:{row.name}", detail={"name": row.name})
    await db.commit()
    await db.refresh(row)
    return {**_json(row), "token": token}


@router.delete("/{token_id}")
async def delete_token(token_id: int, admin_user: dict = Depends(auth.require_superadmin), db: AsyncSession = Depends(get_db)):
    row = await db.get(ApiToken, token_id)
    if not row:
        raise HTTPException(status_code=404, detail="Token not found")
    await log_audit(db, "token.delete", actor=admin_user["username"], detail={"name": row.name})
    await db.delete(row)
    await db.commit()
    return {"ok": True}
