"""Projects: which ones a user may see, with which permissions, and what a project gives its apps.

Access. The root account and a user whose project_access is "all" have their own role in every project. A user
whose project_access is "selected" sees only the projects they are a member of, each with the permissions of the
role given there (none at all without memberships); their own role still decides everything outside projects (nodes,
settings, users). Being a member changes nothing by itself: it is the user's project_access that says which counts. auth.require_permission applies this to every
route with an {app_id} or {database_id}; lists filter with visible_projects().

Shared settings. A project's environment variables are given to every app in it (an app's own variable of the same
name wins), and its apps can reach each other when apps_connected is on. Container starts are synchronous and happen
in many places, so these are kept in memory (refresh() after every change), like the database links.
"""
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import AsyncSessionLocal
from core.env_crypto import decrypt_env
from core.models import Application, Database, Permission, Project, ProjectMember, role_permissions

# project id -> its shared environment variables / whether its apps reach each other
_env: dict[int, dict] = {}
_connected: dict[int, bool] = {}


# ── Access ────────────────────────────────────────────────────────────────────

async def memberships(db: AsyncSession, user) -> Optional[dict[int, set[str]]]:
    """{project id: permissions there} for a user limited to the projects they are a member of; None for a user
    with access to every project."""
    if (user.project_access or "all") == "all":
        return None
    rows = (await db.execute(select(ProjectMember.project_id, ProjectMember.role_id)
                             .where(ProjectMember.user_id == user.id))).all()
    perms_of_role: dict[int, set[str]] = {}
    for role_id in {r for _p, r in rows}:
        perms_of_role[role_id] = set((await db.execute(
            select(Permission.name).join(role_permissions, Permission.id == role_permissions.c.permission_id)
            .where(role_permissions.c.role_id == role_id))).scalars())
    return {project_id: perms_of_role[role_id] for project_id, role_id in rows}


def visible_projects(user: dict) -> Optional[set[int]]:
    """The projects this user may see: None for all of them."""
    scope = user.get("projects")
    return None if scope is None else set(scope)


def can(user: dict, project_id: Optional[int], permission: str) -> bool:
    """Whether the user has this permission in this project."""
    scope = user.get("projects")
    if scope is None:
        return user["permissions"] is None or permission in user["permissions"]
    return project_id in scope and permission in scope[project_id]


async def project_of(db: AsyncSession, *, app_id: Optional[int] = None, database_id: Optional[int] = None) -> Optional[int]:
    if app_id is not None:
        return (await db.execute(select(Application.project_id).where(Application.id == app_id))).scalar_one_or_none()
    if database_id is not None:
        return (await db.execute(select(Database.project_id).where(Database.id == database_id))).scalar_one_or_none()
    return None


async def resource_project(path_params: dict) -> tuple[bool, Optional[int]]:
    """(is this about one app, database or project, its project) from a route's path parameters."""
    def _int(name):
        try:
            return int(path_params[name])
        except (KeyError, TypeError, ValueError):
            return None
    app_id, database_id = _int("app_id"), _int("database_id")
    if app_id is None and database_id is None:
        project_id = _int("project_id")
        return (project_id is not None), project_id
    async with AsyncSessionLocal() as db:
        return True, await project_of(db, app_id=app_id, database_id=database_id)


def visible_apps_query(user: dict):
    """A subquery of the ids of the apps this user may see, or None when they see every app."""
    visible = visible_projects(user)
    if visible is None:
        return None
    return select(Application.id).where(Application.project_id.in_(visible))


async def default_project_id(db: AsyncSession) -> int:
    return (await db.execute(select(Project.id).where(Project.is_default == True))).scalar_one()  # noqa: E712


async def project_for_new(db: AsyncSession, user: dict, requested: Optional[int], permission: str) -> int:
    """The project a new app or database goes into: the one asked for, otherwise the default one (or, for a user
    with memberships, their first project where they may do this). Raises 403/404 when it is not allowed."""
    scope = user.get("projects")
    if requested is None:
        if scope is None:
            return await default_project_id(db)
        allowed = sorted(p for p, perms in scope.items() if permission in perms)
        if not allowed:
            raise HTTPException(403, f"Permission '{permission}' required")
        return allowed[0]
    if await db.get(Project, requested) is None or (scope is not None and requested not in scope):
        raise HTTPException(404, "Project not found")
    if not can(user, requested, permission):
        raise HTTPException(403, f"Permission '{permission}' required in this project")
    return requested


# ── Shared settings ───────────────────────────────────────────────────────────

async def refresh(db: Optional[AsyncSession] = None) -> None:
    """Reload what projects give their apps (after a project changed, and at startup)."""
    async def load(session: AsyncSession) -> None:
        rows = (await session.execute(select(Project))).scalars().all()
        _env.clear()
        _connected.clear()
        for p in rows:
            _env[p.id] = decrypt_env(p.env_vars or "")
            _connected[p.id] = bool(p.apps_connected)
    if db is not None:
        await load(db)
    else:
        async with AsyncSessionLocal() as session:
            await load(session)


def shared_env(project_id: Optional[int]) -> dict:
    return dict(_env.get(project_id) or {})


def apps_connected(project_id: Optional[int]) -> bool:
    return bool(_connected.get(project_id))


def app_env(app: Application) -> dict:
    """The environment of an app's containers: its project's shared variables, then its own (which win)."""
    return {**shared_env(app.project_id), **decrypt_env(app.env_vars or "")}
