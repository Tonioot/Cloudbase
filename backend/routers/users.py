from typing import Optional
from fastapi import HTTPException, Depends, APIRouter
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import get_db
from core.models import User, Role
from core.audit import log_audit
from core import auth

router = APIRouter()


# ── User management endpoints (superadmin only) ──────────────────────────────
class CreateUserRequest(BaseModel):
    username: str
    password: str
    role_id: int


@router.get("/api/users")
async def list_users(_user: dict = Depends(auth.require_superadmin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).order_by(User.created_at))
    users = result.scalars().all()
    out = []
    for u in users:
        # The Root account has no assignable role — it always has full access
        if u.username == "admin":
            out.append({
                "id": u.id,
                "username": u.username,
                "role": "Root",
                "role_id": None,
                "is_root": True,
                "created_at": u.created_at.isoformat() if u.created_at else None,
            })
            continue
        role_name = u.role
        if u.role_id:
            role_res = await db.execute(select(Role).where(Role.id == u.role_id))
            role_obj = role_res.scalar_one_or_none()
            if role_obj:
                role_name = role_obj.name
        out.append({
            "id": u.id,
            "username": u.username,
            "role": role_name,
            "role_id": u.role_id,
            "is_root": False,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        })
    return out


@router.post("/api/users")
async def create_user(req: CreateUserRequest, admin_user: dict = Depends(auth.require_superadmin), db: AsyncSession = Depends(get_db)):
    if len(req.username.strip()) < 2:
        raise HTTPException(status_code=400, detail="Username must be at least 2 characters")
    if len(req.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    role_res = await db.execute(select(Role).where(Role.id == req.role_id))
    role_obj = role_res.scalar_one_or_none()
    if not role_obj:
        raise HTTPException(status_code=400, detail="Role not found")
    existing = await db.execute(select(User).where(User.username == req.username.strip()))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Username already exists")
    new_user = User(
        username=req.username.strip(),
        password_hash=auth.hash_password(req.password),
        role=role_obj.name,
        role_id=role_obj.id,
    )
    db.add(new_user)
    await log_audit(db, "user.create", actor=admin_user["username"], detail={"username": req.username, "role": role_obj.name})
    await db.commit()
    await db.refresh(new_user)
    return {"id": new_user.id, "username": new_user.username, "role": role_obj.name, "role_id": new_user.role_id}


class UpdateUserRequest(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None
    role_id: Optional[int] = None


@router.put("/api/users/{user_id}")
async def update_user(user_id: int, req: UpdateUserRequest, current_user: dict = Depends(auth.require_superadmin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    detail: dict = {"username": user.username}
    if req.username is not None:
        new_name = req.username.strip()
        if len(new_name) < 2:
            raise HTTPException(status_code=400, detail="Username must be at least 2 characters")
        if user.username == "admin":
            raise HTTPException(status_code=400, detail="Cannot rename the superadmin account")
        existing = await db.execute(select(User).where(User.username == new_name))
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Username already taken")
        detail["old_username"] = user.username
        user.username = new_name
        detail["new_username"] = new_name
    if req.role_id is not None:
        if user.username == "admin":
            raise HTTPException(status_code=400, detail="Cannot change the superadmin role")
        role_res = await db.execute(select(Role).where(Role.id == req.role_id))
        role_obj = role_res.scalar_one_or_none()
        if not role_obj:
            raise HTTPException(status_code=400, detail="Role not found")
        user.role_id = role_obj.id
        user.role = role_obj.name
        detail["role"] = role_obj.name
        detail["role_id"] = role_obj.id
    if req.password is not None:
        if len(req.password) < 8:
            raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
        user.password_hash = auth.hash_password(req.password)
        user.session_version = (user.session_version or 0) + 1  # sign the user out
        if user.username == "admin":
            auth.save_hashed_password(user.password_hash)
        detail["password_changed"] = True
    await log_audit(db, "user.update", actor=current_user["username"], detail=detail)
    await db.commit()
    role_name = user.role
    if user.role_id:
        role_res = await db.execute(select(Role).where(Role.id == user.role_id))
        role_obj = role_res.scalar_one_or_none()
        if role_obj:
            role_name = role_obj.name
    return {"id": user.id, "username": user.username, "role": role_name, "role_id": user.role_id}


@router.delete("/api/users/{user_id}")
async def delete_user(user_id: int, current_user: dict = Depends(auth.require_superadmin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.username == "admin":
        raise HTTPException(status_code=400, detail="Cannot delete the superadmin account")
    await log_audit(db, "user.delete", actor=current_user["username"], detail={"username": user.username})
    await db.delete(user)
    await db.commit()
    return {"ok": True}
