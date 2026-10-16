"""
Authentication module for Cloudbase.

- Users are stored in the SQLite database with bcrypt-hashed passwords.
- JWTs (HS256) carry username + role_id, expire after 1 hour.
- Tokens are delivered as httpOnly, SameSite=Strict cookies.
- Authorization is permission-based: users have a Role, roles have Permissions.
- The built-in "admin" user is the Root account and always has full access.
"""

import os
import secrets
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Optional

import bcrypt as _bcrypt
from fastapi import Cookie, Depends, HTTPException, Request, status
import jwt
from jwt import PyJWTError as JWTError
import config as _cfg

# ── Config ────────────────────────────────────────────────────────────────────
CREDENTIALS_FILE = os.path.expanduser("~/.cloudbase/credentials")
ALGORITHM = "HS256"


def get_token_expire_seconds() -> int:
    return _cfg.get_auth("token_expire_seconds")


# Legacy constant kept for cookie max_age references; resolved at call time via get_token_expire_seconds().
TOKEN_EXPIRE_SECONDS = 3600

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


# ── Internal agent token (for node_agent.py to authenticate locally) ──────────
_AGENT_TOKEN_FILE = os.path.expanduser("~/.cloudbase/agent_token")


def get_or_create_agent_token() -> str:
    """Return existing agent token or create a new one."""
    os.makedirs(os.path.dirname(_AGENT_TOKEN_FILE), exist_ok=True)
    if os.path.exists(_AGENT_TOKEN_FILE):
        with open(_AGENT_TOKEN_FILE) as f:
            tok = f.read().strip()
        if tok:
            return tok
    tok = secrets.token_hex(32)
    with open(_AGENT_TOKEN_FILE, "w") as f:
        f.write(tok)
    os.chmod(_AGENT_TOKEN_FILE, 0o600)
    return tok


def verify_agent_token(token: str) -> bool:
    """Return True if the token matches the stored agent token."""
    if not token:
        return False
    try:
        if not os.path.exists(_AGENT_TOKEN_FILE):
            return False
        with open(_AGENT_TOKEN_FILE) as f:
            stored = f.read().strip()
        return secrets.compare_digest(token, stored)
    except Exception:
        return False


def _decode_payload(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


def decode_token(token: str) -> Optional[dict]:
    """Return {"username": str, "role": str} if valid, else None.
    Legacy tokens with sub=="admin" and no role field are treated as admin."""
    payload = _decode_payload(token)
    if payload is None:
        return None
    username = payload.get("sub")
    if not username:
        return None
    role = payload.get("role", "admin" if username == "admin" else "viewer")
    return {"username": username, "role": role, "sv": int(payload.get("sv", 0) or 0)}


async def session_user(token: Optional[str]) -> Optional[dict]:
    """Like decode_token, but also requires the user to still exist and the
    session not to be revoked (logout / password change since it was issued)."""
    user = decode_token(token) if token else None
    if not user:
        return None
    db_user = await _get_db_user(user["username"])
    if db_user is None or int(db_user.session_version or 0) != user["sv"]:
        return None
    return {**user, "db_user": db_user}


async def get_user_permissions(username: str) -> set[str]:
    """Return the set of permission names for a user, loaded live from the DB."""
    from database import AsyncSessionLocal
    from models import User, Role, Permission, role_permissions
    from sqlalchemy import select
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.username == username))
            user = result.scalar_one_or_none()
            if user is None:
                return set()
            # Superadmin always has all permissions
            if username == "admin":
                res = await db.execute(select(Permission.name))
                return {row[0] for row in res.fetchall()}
            if user.role_id is None:
                return set()
            res = await db.execute(
                select(Permission.name)
                .join(role_permissions, Permission.id == role_permissions.c.permission_id)
                .where(role_permissions.c.role_id == user.role_id)
            )
            return {row[0] for row in res.fetchall()}
    except Exception:
        return set()


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
def origin_allowed(origin: Optional[str], host: Optional[str]) -> bool:
    """True when a request either has no Origin (not from a browser page) or
    comes from the panel's own origin. Apps on a sibling subdomain are same-site,
    so the SameSite cookie alone does not stop them."""
    if not origin:
        return True
    from urllib.parse import urlsplit
    try:
        netloc = urlsplit(origin).netloc.lower()
    except ValueError:
        return False
    return bool(netloc) and netloc == (host or "").lower()


# ── FastAPI dependencies ──────────────────────────────────────────────────────
_COOKIE_NAME = "pdm_token"


async def require_auth(pdm_token: Optional[str] = Cookie(default=None)) -> dict:
    """Require a valid session. Returns {"username": str, "role": str}."""
    user = await session_user(pdm_token)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return {"username": user["username"], "role": user["role"]}


async def _get_db_user(username: str):
    """Return the live DB User object or None."""
    from database import AsyncSessionLocal
    from models import User
    from sqlalchemy import select
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.username == username))
            return result.scalar_one_or_none()
    except Exception:
        return None


async def require_admin(pdm_token: Optional[str] = Cookie(default=None)) -> dict:
    """Require the user to have the built-in superadmin account OR the 'roles.manage'/'users.manage' scope.
    For backwards compatibility this still checks for full admin (all permissions)."""
    user = await session_user(pdm_token)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    db_user = user.pop("db_user")
    # Superadmin always passes
    if db_user.username == "admin":
        return {**user, "role": db_user.role, "role_id": db_user.role_id, "permissions": None}
    perms = await get_user_permissions(db_user.username)
    # "admin" role means all permissions granted — check by seeing if system.manage is present
    if "system.manage" not in perms:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return {**user, "role": db_user.role, "role_id": db_user.role_id, "permissions": perms}


async def require_superadmin(pdm_token: Optional[str] = Cookie(default=None)) -> dict:
    """Require the built-in 'admin' superuser account. Only this account may manage users and roles."""
    user = await session_user(pdm_token)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    db_user = user.pop("db_user")
    if db_user.username != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the superadmin account can manage users and roles")
    return {**user, "role": db_user.role, "role_id": db_user.role_id, "permissions": None}


def require_permission(permission: str):
    """Return a FastAPI dependency that requires the user to have a specific permission."""
    async def _dep(pdm_token: Optional[str] = Cookie(default=None)) -> dict:
        user = await session_user(pdm_token)
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
        db_user = user.pop("db_user")
        # Superadmin bypasses all permission checks
        if db_user.username == "admin":
            return {**user, "role": db_user.role, "role_id": db_user.role_id, "permissions": None}
        perms = await get_user_permissions(db_user.username)
        if permission not in perms:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission '{permission}' required",
            )
        return {**user, "role": db_user.role, "role_id": db_user.role_id, "permissions": perms}
    return _dep


def get_current_actor(pdm_token: Optional[str] = Cookie(default=None)) -> str:
    """Return the username of the current session, or 'system' for agent/unauthenticated calls."""
    if not pdm_token:
        return "system"
    user = decode_token(pdm_token)
    return user["username"] if user else "system"


async def authorize_websocket(websocket, required_permission: str | None = None) -> bool:
    """
    Validate a WebSocket connection. Accepts either:
    - Browser session: pdm_token cookie (user JWT) with optional permission check.
    - Node agent: X-Node-Token header (stored in Node.auth_token) — bypasses permission checks.
    - Local agent: X-Agent-Token header — bypasses permission checks.

    Returns True if allowed, False if rejected (connection closed with 1008).
    """
    # Local agent token (node_agent.py on the same machine)
    agent_token = websocket.headers.get("x-agent-token", "")
    if agent_token:
        if verify_agent_token(agent_token):
            return True
        await websocket.close(code=1008, reason="Invalid agent token")
        return False

    # Remote node agent token
    node_token = websocket.headers.get("x-node-token", "")
    if node_token:
        from database import AsyncSessionLocal
        from models import Node
        from sqlalchemy import select
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(Node).where(Node.auth_token == node_token, Node.enabled == True)
                )
                if result.scalar_one_or_none() is not None:
                    return True
        except Exception:
            pass
        await websocket.close(code=1008, reason="Invalid node token")
        return False

    # Browser session cookie — only from the panel's own pages
    if not origin_allowed(websocket.headers.get("origin"), websocket.headers.get("host")):
        await websocket.close(code=1008, reason="Cross-origin WebSocket refused")
        return False
    user = await session_user(websocket.cookies.get(_COOKIE_NAME))
    if not user:
        await websocket.close(code=1008, reason="Not authenticated")
        return False
    if user.get("username") == "admin":
        return True
    if required_permission is not None:
        perms = await get_user_permissions(user["username"])
        if required_permission not in perms:
            await websocket.close(code=1008, reason="Permission denied")
            return False
    return True
