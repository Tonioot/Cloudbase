"""Projects: groups of apps and databases with their own members and shared settings (see core/projects.py)."""
import re
from collections import Counter
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core import auth, projects as core_projects
from core.audit import log_audit
from core.database import get_db
from core.env_crypto import decrypt_env, encrypt_env
from core.models import Application, Database, Instance, Project, ProjectMember, Role, User

router = APIRouter(prefix="/api/projects", tags=["projects"])

NAME_RE = re.compile(r"^[\w][\w .\-]{0,59}$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ProjectRequest(BaseModel):
    name: str = Field(..., max_length=60)
    description: Optional[str] = Field(None, max_length=300)


class ProjectUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=60)
    description: Optional[str] = Field(None, max_length=300)
    apps_connected: Optional[bool] = None


class EnvRequest(BaseModel):
    env: dict[str, str]


class Member(BaseModel):
    user_id: int
    role_id: int


class MembersRequest(BaseModel):
    members: list[Member]


class MoveRequest(BaseModel):
    project_id: int


def _clean_name(name: str) -> str:
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise HTTPException(400, "A project name is 1–60 letters, digits, spaces, dots, dashes or underscores")
    return name


async def _project_or_404(db: AsyncSession, project_id: int) -> Project:
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    return project


async def _name_free(db: AsyncSession, name: str, own_id: Optional[int] = None) -> None:
    other = (await db.execute(select(Project.id).where(func.lower(Project.name) == name.lower()))).scalar_one_or_none()
    if other is not None and other != own_id:
        raise HTTPException(409, f"There is already a project called '{name}'")


def _summary(p: Project, apps: Counter, running: Counter, databases: Counter, user: dict) -> dict:
    return {
        "id": p.id, "name": p.name, "description": p.description, "is_default": bool(p.is_default),
        "apps_connected": bool(p.apps_connected),
        "apps": apps.get(p.id, 0), "apps_running": running.get(p.id, 0), "databases": databases.get(p.id, 0),
        "env_count": len(decrypt_env(p.env_vars or "")),
        # what this user may do here, so the panel shows only what works
        "can": {perm: core_projects.can(user, p.id, perm) for perm in
                ("apps.view", "apps.deploy", "apps.configure", "databases.view", "databases.create", "databases.manage",
                 "projects.manage")},
    }


# ── Listing and reading ───────────────────────────────────────────────────────

@router.get("")
async def list_projects(db: AsyncSession = Depends(get_db), user: dict = Depends(auth.require_project_permission("apps.view"))):
    visible = core_projects.visible_projects(user)
    rows = (await db.execute(select(Project).order_by(Project.is_default.desc(), Project.name))).scalars().all()
    rows = [p for p in rows if visible is None or p.id in visible]
    apps = Counter(dict((await db.execute(select(Application.project_id, func.count()).group_by(Application.project_id))).all()))
    databases = Counter(dict((await db.execute(select(Database.project_id, func.count()).group_by(Database.project_id))).all()))
    running = Counter(dict((await db.execute(
        select(Application.project_id, func.count(func.distinct(Application.id)))
        .join(Instance, Instance.app_id == Application.id).where(Instance.status == "running")
        .group_by(Application.project_id))).all()))
    return [_summary(p, apps, running, databases, user) for p in rows]


@router.get("/{project_id}")
async def get_project(project_id: int, db: AsyncSession = Depends(get_db),
                      user: dict = Depends(auth.require_permission("apps.view"))):
    project = await _project_or_404(db, project_id)
    apps = (await db.execute(select(Application).where(Application.project_id == project_id).order_by(Application.name))).scalars().all()
    databases = (await db.execute(select(Database).where(Database.project_id == project_id).order_by(Database.name))).scalars().all()
    instances = (await db.execute(select(Instance).where(Instance.app_id.in_([a.id for a in apps])))).scalars().all() if apps else []
    by_app: dict[int, list[Instance]] = {}
    for i in instances:
        by_app.setdefault(i.app_id, []).append(i)
    summary = _summary(project, Counter({project.id: len(apps)}),
                       Counter({project.id: sum(1 for a in apps if any(i.status == "running" for i in by_app.get(a.id, [])))}),
                       Counter({project.id: len(databases)}), user)
    can_manage = core_projects.can(user, project_id, "projects.manage")
    return {
        **summary,
        "env_keys": sorted(decrypt_env(project.env_vars or "")) if can_manage else [],
        "app_list": [{
            "id": a.id, "name": a.name, "domain": a.domain, "app_type": a.app_type,
            "kind": "cron job" if a.cron_schedule else "static" if a.app_type == "static" else "worker" if a.no_web else "web service",
            "instances": len(by_app.get(a.id, [])),
            "running": sum(1 for i in by_app.get(a.id, []) if i.status == "running"),
            "statuses": sorted({i.status for i in by_app.get(a.id, [])}),
            "cpu_limit": a.docker_cpu_limit, "memory_limit_mb": a.docker_memory_limit_mb,
        } for a in apps],
        "database_list": [{"id": d.id, "name": d.name, "engine": d.engine, "version": d.version, "status": d.status}
                          for d in databases] if core_projects.can(user, project_id, "databases.view") else [],
    }


# ── Creating, changing, deleting ──────────────────────────────────────────────

@router.post("")
async def create_project(req: ProjectRequest, db: AsyncSession = Depends(get_db),
                         user: dict = Depends(auth.require_permission("projects.manage")),
                         actor: str = Depends(auth.get_current_actor)):
    if user.get("projects") is not None:
        raise HTTPException(403, "Only users with access to every project can create projects")
    name = _clean_name(req.name)
    await _name_free(db, name)
    project = Project(name=name, description=(req.description or "").strip() or None,
                      apps_connected=True)                    # a new project's apps find each other
    db.add(project)
    await db.flush()
    await log_audit(db, "project.create", actor=actor, detail={"name": name, "project_id": project.id})
    await db.commit()
    await core_projects.refresh()
    return {"id": project.id, "name": project.name}


@router.patch("/{project_id}")
async def update_project(project_id: int, req: ProjectUpdate, db: AsyncSession = Depends(get_db),
                         _user: dict = Depends(auth.require_permission("projects.manage")),
                         actor: str = Depends(auth.get_current_actor)):
    project = await _project_or_404(db, project_id)
    changed = {}
    if req.name is not None:
        name = _clean_name(req.name)
        await _name_free(db, name, project.id)
        project.name, changed["name"] = name, name
    if req.description is not None:
        project.description = req.description.strip() or None
        changed["description"] = project.description
    if req.apps_connected is not None:
        project.apps_connected = req.apps_connected
        changed["apps_connected"] = req.apps_connected
    await log_audit(db, "project.update", actor=actor, detail={"project_id": project_id, **changed})
    await db.commit()
    await core_projects.refresh()
    return {"ok": True}


@router.delete("/{project_id}")
async def delete_project(project_id: int, db: AsyncSession = Depends(get_db),
                         _user: dict = Depends(auth.require_permission("projects.manage")),
                         actor: str = Depends(auth.get_current_actor)):
    project = await _project_or_404(db, project_id)
    if project.is_default:
        raise HTTPException(400, "The default project cannot be deleted")
    apps = (await db.execute(select(func.count()).where(Application.project_id == project_id))).scalar_one()
    databases = (await db.execute(select(func.count()).where(Database.project_id == project_id))).scalar_one()
    if apps or databases:
        raise HTTPException(409, "Move or delete the project's apps and databases first")
    name = project.name
    await db.delete(project)
    await log_audit(db, "project.delete", actor=actor, detail={"name": name, "project_id": project_id})
    await db.commit()
    await core_projects.refresh()
    return {"ok": True}


# ── Shared environment variables ──────────────────────────────────────────────

@router.get("/{project_id}/env")
async def get_env(project_id: int, db: AsyncSession = Depends(get_db),
                  _user: dict = Depends(auth.require_permission("projects.manage"))):
    project = await _project_or_404(db, project_id)
    return {"env": decrypt_env(project.env_vars or "")}


@router.put("/{project_id}/env")
async def set_env(project_id: int, req: EnvRequest, db: AsyncSession = Depends(get_db),
                  _user: dict = Depends(auth.require_permission("projects.manage")),
                  actor: str = Depends(auth.get_current_actor)):
    project = await _project_or_404(db, project_id)
    env = {k.strip(): str(v) for k, v in req.env.items() if k.strip()}
    bad = [k for k in env if not ENV_KEY_RE.match(k)]
    if bad:
        raise HTTPException(400, f"Not a valid variable name: {', '.join(bad)}")
    project.env_vars = encrypt_env(env) if env else None
    await log_audit(db, "project.env", actor=actor, detail={"project_id": project_id, "keys": sorted(env)})
    await db.commit()
    await core_projects.refresh()
    return {"ok": True, "keys": sorted(env)}


# ── Members (the root account manages users, so it manages memberships too) ─────

@router.get("/{project_id}/members")
async def get_members(project_id: int, db: AsyncSession = Depends(get_db),
                      _admin: dict = Depends(auth.require_superadmin)):
    await _project_or_404(db, project_id)
    rows = (await db.execute(
        select(ProjectMember, User, Role).join(User, User.id == ProjectMember.user_id)
        .join(Role, Role.id == ProjectMember.role_id).where(ProjectMember.project_id == project_id)
        .order_by(User.username))).all()
    return [{"user_id": u.id, "username": u.username, "role_id": r.id, "role": r.name} for _m, u, r in rows]


@router.put("/{project_id}/members")
async def set_members(project_id: int, req: MembersRequest, db: AsyncSession = Depends(get_db),
                      _admin: dict = Depends(auth.require_superadmin),
                      actor: str = Depends(auth.get_current_actor)):
    await _project_or_404(db, project_id)
    wanted = {m.user_id: m.role_id for m in req.members}
    users = {u.id: u for u in (await db.execute(select(User).where(User.id.in_(wanted)))).scalars()} if wanted else {}
    roles = set((await db.execute(select(Role.id).where(Role.id.in_(set(wanted.values()))))).scalars()) if wanted else set()
    for user_id, role_id in wanted.items():
        if user_id not in users:
            raise HTTPException(404, f"User {user_id} not found")
        if users[user_id].username == "admin":
            raise HTTPException(400, "The admin account always has every project")
        if role_id not in roles:
            raise HTTPException(404, f"Role {role_id} not found")
    current = {m.user_id: m for m in (await db.execute(
        select(ProjectMember).where(ProjectMember.project_id == project_id))).scalars()}
    for user_id, member in current.items():
        if user_id not in wanted:
            await db.delete(member)
        else:
            member.role_id = wanted[user_id]
    for user_id, role_id in wanted.items():
        if user_id not in current:
            db.add(ProjectMember(project_id=project_id, user_id=user_id, role_id=role_id))
    await log_audit(db, "project.members", actor=actor,
                    detail={"project_id": project_id, "members": {users[u].username: r for u, r in wanted.items()}})
    await db.commit()
    return {"ok": True}


# ── Moving an app or database to another project ──────────────────────────────

async def _check_target(db: AsyncSession, user: dict, project_id: int, permission: str) -> Project:
    project = await db.get(Project, project_id)
    if project is None or not core_projects.can(user, project_id, "apps.view"):
        raise HTTPException(404, "Project not found")
    if not core_projects.can(user, project_id, permission):
        raise HTTPException(403, f"Permission '{permission}' required in the project it moves to")
    return project


@router.post("/move/app/{app_id}")
async def move_app(app_id: int, req: MoveRequest, db: AsyncSession = Depends(get_db),
                   user: dict = Depends(auth.require_permission("apps.configure")),
                   actor: str = Depends(auth.get_current_actor)):
    app = await db.get(Application, app_id)
    if app is None:
        raise HTTPException(404, "App not found")
    target = await _check_target(db, user, req.project_id, "apps.configure")
    app.project_id = target.id
    await log_audit(db, "app.move", actor=actor, app_id=app_id, detail={"name": app.name, "project": target.name})
    await db.commit()
    return {"ok": True, "project_id": target.id}


@router.post("/move/database/{database_id}")
async def move_database(database_id: int, req: MoveRequest, db: AsyncSession = Depends(get_db),
                        user: dict = Depends(auth.require_permission("databases.manage")),
                        actor: str = Depends(auth.get_current_actor)):
    database = await db.get(Database, database_id)
    if database is None:
        raise HTTPException(404, "Database not found")
    target = await _check_target(db, user, req.project_id, "databases.manage")
    database.project_id = target.id
    await log_audit(db, "database.move", actor=actor, detail={"name": database.name, "project": target.name})
    await db.commit()
    return {"ok": True, "project_id": target.id}
