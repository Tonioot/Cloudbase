from fastapi import Request, HTTPException, Depends, APIRouter
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import get_db, AsyncSessionLocal
from core.models import User, Permission, Role, role_permissions
from core.audit import log_audit
from core import auth
from core.runtime import _COOKIE_NAME, _COOKIE_OPTS

router = APIRouter()


# ── Auth endpoints (public) ───────────────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str
    password: str


@router.get("/api/auth/check")
async def auth_check(request: Request, db: AsyncSession = Depends(get_db)):
    user = await auth.session_user(request.cookies.get(_COOKIE_NAME))
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    db_user = user["db_user"]
    is_root = db_user.username == "admin"
    perms: list[str] = []
    if is_root:
        perm_res = await db.execute(select(Permission.name))
        perms = [row[0] for row in perm_res.fetchall()]
        return {
            "authenticated": True,
            "username": db_user.username,
            "role": "Root",
            "role_id": None,
            "is_root": True,
            "permissions": perms,
        }
    if db_user.role_id:
        perm_res = await db.execute(
            select(Permission.name)
            .join(role_permissions, Permission.id == role_permissions.c.permission_id)
            .where(role_permissions.c.role_id == db_user.role_id)
        )
        perms = [row[0] for row in perm_res.fetchall()]
    # Fetch role name
    role_name = db_user.role
    if db_user.role_id:
        role_res = await db.execute(select(Role).where(Role.id == db_user.role_id))
        role_obj = role_res.scalar_one_or_none()
        if role_obj:
            role_name = role_obj.name
    return {
        "authenticated": True,
        "username": db_user.username,
        "role": role_name,
        "role_id": db_user.role_id,
        "is_root": False,
        "permissions": perms,
    }


@router.post("/api/auth/login")
async def login(req: LoginRequest, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    client_ip = request.client.host if request.client else "unknown"
    if client_ip in ("127.0.0.1", "::1"):
        # Through the local nginx: limit per visitor, not one shared bucket for everyone
        client_ip = request.headers.get("x-real-ip") or client_ip
    auth._check_rate_limit(client_ip)
    result = await db.execute(select(User).where(User.username == req.username))
    user = result.scalar_one_or_none()
    if not user or not auth.verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = auth.create_access_token(user.username, user.role, user.session_version)
    expire_seconds = auth.get_token_expire_seconds()
    # Secure over HTTPS (nginx sets X-Forwarded-Proto); plain HTTP on an IP keeps working
    https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(key=_COOKIE_NAME, value=token, max_age=expire_seconds, secure=https, **_COOKIE_OPTS)
    await log_audit(db, "auth.login", actor=user.username, detail={"ip": client_ip})
    await db.commit()
    return {"ok": True, "expires_in": expire_seconds, "role": user.role}


@router.get("/api/auth/session")
async def session_info(request: Request):
    token = request.cookies.get(_COOKIE_NAME)
    if not await auth.session_user(token):
        raise HTTPException(status_code=401, detail="Not authenticated")
    expires_in = auth.get_token_expires_in(token)
    if expires_in is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"expires_in": expires_in}


@router.post("/api/auth/logout")
async def logout(response: Response, request: Request):
    response.delete_cookie(key=_COOKIE_NAME, path="/")
    session = await auth.session_user(request.cookies.get(_COOKIE_NAME))
    actor = session["username"] if session else "system"
    async with AsyncSessionLocal() as db:
        if session:
            # Revoke the token itself (it could have been copied), which signs
            # this user out everywhere
            db_user = await db.get(User, session["db_user"].id)
            if db_user:
                db_user.session_version = (db_user.session_version or 0) + 1
        await log_audit(db, "auth.logout", actor=actor)
        await db.commit()
    return {"ok": True}


class ChangePasswordRequest(BaseModel):
    password: str


@router.post("/api/auth/change-password")
async def change_password(req: ChangePasswordRequest, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    user_info = await auth.session_user(request.cookies.get(_COOKIE_NAME))
    if not user_info:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if len(req.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    result = await db.execute(select(User).where(User.username == user_info["username"]))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.password_hash = auth.hash_password(req.password)
    # Sign out every other session; this one gets a fresh token below
    user.session_version = (user.session_version or 0) + 1
    # Keep legacy credentials file in sync for the admin user
    if user.username == "admin":
        auth.save_hashed_password(user.password_hash)
    await log_audit(db, "auth.change_password", actor=user.username, detail={"username": user.username})
    await db.commit()
    expire_seconds = auth.get_token_expire_seconds()
    https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(key=_COOKIE_NAME, value=auth.create_access_token(user.username, user.role, user.session_version),
                        max_age=expire_seconds, secure=https, **_COOKIE_OPTS)
    return {"ok": True}
