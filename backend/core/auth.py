"""
Authentication module for Cloudbase.

- Users are stored in the SQLite database with bcrypt-hashed passwords.
- JWTs (HS256) carry username + role_id, expire after 1 hour.
- Tokens are delivered as httpOnly, SameSite=Strict cookies.
- Authorization is permission-based: users have a Role, roles have Permissions.
- The built-in "admin" user is the Root account and always has full access.
"""

import hashlib
import json
import os
import secrets
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Optional

import bcrypt as _bcrypt
from fastapi import HTTPException, Request, status
import jwt
from jwt import PyJWTError as JWTError
from core import config as _cfg
from core.database import AsyncSessionLocal
from core.models import ApiToken, User, Permission, role_permissions
from core import system_config as _syscfg
from core import projects

# ── Config ────────────────────────────────────────────────────────────────────
CREDENTIALS_FILE = os.path.expanduser("~/.cloudbase/credentials")
ALGORITHM = "HS256"


def get_token_expire_seconds() -> int:
    return _cfg.get_auth("token_expire_seconds")


# ── Secret key (generated once, stored alongside credentials) ─────────────────
_SECRET_KEY_FILE = os.path.expanduser("~/.cloudbase/secret_key")


def _load_secret_key() -> str:
    os.makedirs(os.path.dirname(_SECRET_KEY_FILE), exist_ok=True)
    if os.path.exists(_SECRET_KEY_FILE):
        with open(_SECRET_KEY_FILE) as f:
            return f.read().strip()
    key = secrets.token_hex(48)
    with open(_SECRET_KEY_FILE, "w") as f:
        f.write(key)
    os.chmod(_SECRET_KEY_FILE, 0o600)
    return key


SECRET_KEY: str = _load_secret_key()


# ── Password helpers ──────────────────────────────────────────────────────────
def hash_password(plain: str) -> str:
    return _bcrypt.hashpw(plain.encode(), _bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


def load_hashed_password() -> Optional[str]:
    if not os.path.exists(CREDENTIALS_FILE):
        return None
    with open(CREDENTIALS_FILE) as f:
        return f.read().strip() or None


def save_hashed_password(hashed: str) -> None:
    os.makedirs(os.path.dirname(CREDENTIALS_FILE), exist_ok=True)
    with open(CREDENTIALS_FILE, "w") as f:
        f.write(hashed)
    os.chmod(CREDENTIALS_FILE, 0o600)


# ── JWT helpers ───────────────────────────────────────────────────────────────
def create_access_token(username: str, role: str, session_version: int = 0) -> str:
    expire_seconds = get_token_expire_seconds()
    expire = datetime.now(timezone.utc) + timedelta(seconds=expire_seconds)
    payload = {
        "sub": username,
        "role": role,
        "sv": int(session_version or 0),
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def _decode_payload(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


def decode_token(token: str) -> Optional[dict]:
    """Return {"username": str, "role": str, "sv": int} if valid, else None."""
    payload = _decode_payload(token)
    if payload is None:
        return None
    username = payload.get("sub")
    if not username:
        return None
    role = payload.get("role", "viewer")
    return {"username": username, "role": role, "sv": int(payload.get("sv", 0) or 0)}


async def _role_permissions(db, role_id: Optional[int]) -> set[str]:
    if role_id is None:
        return set()
    from sqlalchemy import select
    return set((await db.execute(
        select(Permission.name)
        .join(role_permissions, Permission.id == role_permissions.c.permission_id)
        .where(role_permissions.c.role_id == role_id)
    )).scalars())


async def _load_user(db, db_user: User, user: dict) -> dict:
    """What the endpoints need to know about a signed-in user: their permissions and the projects they are limited to."""
    # The root account has every permission; None: every project with the user's own permissions, otherwise
    # {project id: permissions there}
    permissions = None if db_user.username == "admin" else await _role_permissions(db, db_user.role_id)
    scope = None if db_user.username == "admin" else await projects.memberships(db, db_user)
    return {**user, "db_user": db_user, "permissions": permissions, "projects": scope}


async def session_user(token: Optional[str]) -> Optional[dict]:
    """The signed-in user, looked up in one go: {username, role, sv, db_user, permissions}, where permissions is None
    for the root account (everything) and a set otherwise. None when the token is invalid, the user is gone or the
    session was revoked (logout / password change since it was issued)."""
    user = decode_token(token) if token else None
    if not user:
        return None
    from sqlalchemy import select
    try:
        async with AsyncSessionLocal() as db:
            db_user = (await db.execute(select(User).where(User.username == user["username"]))).scalar_one_or_none()
            if db_user is None or int(db_user.session_version or 0) != user["sv"]:
                return None
            return await _load_user(db, db_user, user)
    except Exception:
        return None


# ── API tokens ────────────────────────────────────────────────────────────────
TOKEN_PREFIX = "cbt_"
_LAST_USED_EVERY = timedelta(minutes=5)     # last_used_at is a hint, not a log: one write per few minutes is enough


def new_api_token() -> tuple[str, str, str]:
    """(the token, its hash, its prefix). The token is shown once; only the hash is stored."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(48)
    return token, hash_api_token(token), token[:len(TOKEN_PREFIX) + 4]


def hash_api_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def bearer_token(headers) -> Optional[str]:
    """The API token in an Authorization: Bearer header, if there is one."""
    value = headers.get("authorization") or ""
    scheme, _, token = value.partition(" ")
    token = token.strip()
    return token if scheme.lower() == "bearer" and token.startswith(TOKEN_PREFIX) else None


class AuthUnavailable(Exception):
    """The credentials could not be checked (the database did not answer in time): not the same as wrong credentials."""


async def api_token_user(token: str) -> Optional[dict]:
    """Like session_user, for an API token: its own permissions, in every project or only the ones it was made for. None when
    the token is unknown or expired. It acts as "token:<name>", a name no user can have, so it never is the root account.
    Raises AuthUnavailable when the lookup itself fails: a busy database must not look like a deleted token (a client
    would give up on a 401, while it tries a 503 again)."""
    from sqlalchemy import select
    try:
        async with AsyncSessionLocal() as db:
            row = (await db.execute(select(ApiToken).where(ApiToken.token_hash == hash_api_token(token)))).scalar_one_or_none()
            now = datetime.utcnow()
            if row is None or (row.expires_at and row.expires_at <= now):
                return None
            permissions = set(json.loads(row.permissions or "[]"))
            ids = json.loads(row.project_ids) if row.project_ids else []
            username = f"token:{row.name}"
            if row.last_used_at is None or now - row.last_used_at > _LAST_USED_EVERY:
                try:                                # only a hint: a locked database never fails the request for it
                    row.last_used_at = now
                    await db.commit()
                except Exception:
                    await db.rollback()
            return {"username": username, "role": "API token", "sv": 0, "via_token": True,
                    "db_user": User(username=username, role="API token", role_id=None, session_version=0),
                    "permissions": permissions, "projects": {int(i): permissions for i in ids} if ids else None}
    except Exception as exc:
        raise AuthUnavailable(str(exc)) from exc


_UNSET = object()


async def request_user(request) -> Optional[dict]:
    """session_user for this request, looked up once: the middleware does it, the endpoint's checks reuse it."""
    cached = getattr(request.state, "user", _UNSET)
    if cached is _UNSET:
        token = bearer_token(request.headers)
        cached = await api_token_user(token) if token else await session_user(request.cookies.get(_COOKIE_NAME))
        request.state.user = cached
    return cached


def has_permission(user: dict, permission: str) -> bool:
    return user["permissions"] is None or permission in user["permissions"]


def get_token_expires_in(token: str) -> Optional[int]:
    """Return seconds remaining until token expires, or None if invalid."""
    payload = _decode_payload(token)
    if payload is None or not payload.get("sub"):
        return None
    exp = payload.get("exp")
    if exp is None:
        return None
    remaining = int(exp - datetime.now(timezone.utc).timestamp())
    return max(0, remaining)


# ── Rate limiter (in-memory, per IP) ─────────────────────────────────────────
_login_attempts: dict[str, list[float]] = defaultdict(list)
MAX_ATTEMPTS = 5
WINDOW_SECONDS = 60


def _check_rate_limit(ip: str) -> None:
    now = time.time()
    attempts = _login_attempts[ip]
    attempts[:] = [t for t in attempts if now - t < WINDOW_SECONDS]
    if len(attempts) >= MAX_ATTEMPTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many login attempts. Try again in {WINDOW_SECONDS} seconds.",
        )
    attempts.append(now)


# ── Origin check (CSRF / cross-site WebSocket protection) ─────────────────────
def origin_allowed(origin: Optional[str], headers) -> bool:
    """True when a request either has no Origin (not from a browser page) or
    comes from the panel's own origin. Apps on a sibling subdomain are same-site,
    so the SameSite cookie alone does not stop them. The panel's own origin is the
    Host it was asked for, the X-Forwarded-Host of a proxy in front of it (which may
    rewrite Host), or the panel's HTTPS domain."""
    if not origin:
        return True
    from urllib.parse import urlsplit
    try:
        netloc = urlsplit(origin).netloc.lower()
    except ValueError:
        return False
    if not netloc:
        return False
    own = {(headers.get("host") or "").lower()}
    own.update(h.strip().lower() for h in (headers.get("x-forwarded-host") or "").split(",") if h.strip())
    panel_domain = _syscfg.get_panel_https_domain()
    if panel_domain:
        own.add(panel_domain.lower())
    return netloc in own


# ── FastAPI dependencies ──────────────────────────────────────────────────────
_COOKIE_NAME = "cloudbase_token"


async def require_auth(request: Request) -> dict:
    """Require a valid session. Returns {"username": str, "role": str}."""
    user = await request_user(request)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return {"username": user["username"], "role": user["role"]}


def _as_principal(user: dict) -> dict:
    db_user = user["db_user"]
    return {"username": user["username"], "sv": user["sv"], "role": db_user.role, "role_id": db_user.role_id,
            "permissions": user["permissions"], "projects": user.get("projects")}


async def _allowed(user: dict, permission: str, path_params: dict) -> Optional[int]:
    """None when allowed; otherwise the HTTP status to refuse with. On an app or database, a user with project
    memberships is judged by their role in its project, and one outside the project does not get to know it exists."""
    if user.get("projects") is not None:
        about_one, project_id = await projects.resource_project(path_params or {})
        if about_one:
            if project_id not in user["projects"]:
                return status.HTTP_404_NOT_FOUND
            return None if permission in user["projects"][project_id] else status.HTTP_403_FORBIDDEN
    return None if has_permission(user, permission) else status.HTTP_403_FORBIDDEN


async def require_superadmin(request: Request) -> dict:
    """Require the built-in 'admin' superuser account. Only this account may manage users and roles."""
    user = await request_user(request)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    if user["username"] != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the superadmin account can manage users and roles")
    return _as_principal(user)


def require_permission(permission: str):
    """A FastAPI dependency that requires the user to have this permission. This is the one place an endpoint's
    permission is decided; the middleware only checks that somebody is signed in."""
    async def _dep(request: Request) -> dict:
        user = await request_user(request)
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
        refused = await _allowed(user, permission, request.path_params)
        if refused == status.HTTP_404_NOT_FOUND:
            raise HTTPException(status_code=refused, detail="Not found")
        if refused:
            raise HTTPException(status_code=refused, detail=f"Permission '{permission}' required")
        return _as_principal(user)
    return _dep


def require_project_permission(permission: str):
    """For routes about apps or databases in general (a list, creating one): allowed with the permission in at least
    one project. The route itself then filters (projects.can) or checks the project it creates in."""
    async def _dep(request: Request) -> dict:
        user = await request_user(request)
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
        scope = user.get("projects")
        if not (has_permission(user, permission) if scope is None else any(permission in p for p in scope.values())):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Permission '{permission}' required")
        return _as_principal(user)
    return _dep


async def get_current_actor(request: Request) -> str:
    """The username of whoever makes this request (a session or an API token), or 'system' for agent calls."""
    user = await request_user(request)
    return user["username"] if user else "system"


async def authorize_websocket(websocket, required_permission: str | None = None) -> bool:
    """
    Validate a WebSocket connection of the panel: a session cookie (user JWT), with an optional permission check.
    A node token gets nothing here: agents use their own websocket (/api/nodes/ws/agent), and a node token passed
    every check of these (every app's logs and stats, the server logs), which a node that was taken over must not.

    Returns True if allowed, False if rejected (connection closed with 1008).
    """
    # Browser session cookie — only from the panel's own pages
    if not origin_allowed(websocket.headers.get("origin"), websocket.headers):
        await websocket.close(code=1008, reason="Cross-origin WebSocket refused")
        return False
    user = await session_user(websocket.cookies.get(_COOKIE_NAME))
    if not user:
        await websocket.close(code=1008, reason="Not authenticated")
        return False
    if required_permission is not None and await _allowed(user, required_permission, websocket.path_params):
        await websocket.close(code=1008, reason="Permission denied")
        return False
    return True
