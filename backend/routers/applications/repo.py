import asyncio
import logging
import os
import re
from fastapi import HTTPException, Query, Depends, APIRouter
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import get_db
from containers import processes as pm, manager as dm
from core import auth as _auth
from apps.git import _current_branch, _decrypt_github_token, _fetch_origin, _git_app_dir_or_404, _ls_remote, _normalize_repo_url, _recent_git_commits, _resolve_token
from apps.images import _dockerfile_build_kwargs, _image_port
from apps.serializers import _get_or_404
from routers.applications.deps import _source_archive_auth
from routers.applications.schemas import CheckRepoRequest

router = APIRouter(prefix="/api/apps", tags=["applications"])


log = logging.getLogger("cloudbase.apps")


@router.post("/check-repo")
async def check_repo(req: CheckRepoRequest, _user: dict = Depends(_auth.require_permission("apps.deploy"))):
    """Validate a repository URL (and token) before an app is created."""
    url = _normalize_repo_url(req.repo_url)
    if url.startswith("git@"):
        return {"ok": False, "repo_url": url, "message": "SSH links aren’t supported. Use the HTTPS link, e.g. https://github.com/user/repo."}
    if not re.match(r"^https?://[^/\s]+/[^\s]+$", url):
        return {"ok": False, "repo_url": url, "message": "That doesn’t look like a repository link, e.g. https://github.com/user/repo."}
    token = _resolve_token(req.github_token, req.github_token_id)
    ok, msg, default_branch, branches = await asyncio.to_thread(_ls_remote, url, token)
    return {"ok": ok, "repo_url": url, "message": msg, "default_branch": default_branch, "branches": branches[:200]}


@router.get("/{app_id}/source-archive")
async def get_source_archive(app_id: int, db: AsyncSession = Depends(get_db), _node: None = Depends(_source_archive_auth)):
    """Return a .tar.gz of the app's working directory for remote nodes to build from.
    Only the local primary serves this — remote nodes call this endpoint directly."""
    app = await _get_or_404(app_id, db)
    source_dir = pm.app_source_dir(app)
    if not source_dir or not os.path.exists(source_dir):
        raise HTTPException(404, "App source directory not found — deploy the app first")

    import tarfile
    import io

    # Refresh the generated Dockerfile first so the node receives the current
    # one (with marker). Nodes get no .git, so they treat an unmarked
    # Dockerfile as the repository's own.
    try:
        await asyncio.to_thread(
            dm.ensure_dockerfile,
            source_dir,
            app.app_type or "unknown",
            app.start_command or "",
            _image_port(app),
            **_dockerfile_build_kwargs(app),
        )
    except Exception as exc:
        log.warning("[source-archive] Could not refresh Dockerfile for app=%s: %s", app.name, exc)

    def _make_archive() -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(source_dir, arcname=".", recursive=True,
                    filter=lambda m: None if os.path.basename(m.name) in {".git", "__pycache__", "node_modules", ".venv", "venv"} else m)
        return buf.getvalue()

    data = await asyncio.to_thread(_make_archive)
    return Response(
        content=data,
        media_type="application/gzip",
        headers={"Content-Disposition": f"attachment; filename={app.name}-source.tar.gz"},
    )


@router.get("/{app_id}/commits")
async def list_git_commits(
    app_id: int,
    limit: int = Query(20, ge=1, le=100),
    refresh: bool = Query(True),
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("apps.view")),
):
    app = await _get_or_404(app_id, db)
    app_dir = _git_app_dir_or_404(app.name)

    def _get_commits() -> tuple[str, str, list]:
        branch = _current_branch(app_dir)
        ref = "HEAD"
        if refresh:
            _fetch_origin(app_dir, branch)
            ref = f"origin/{branch}"
        commits = _recent_git_commits(app_dir, limit, ref=ref)
        if refresh and not commits:
            commits = _recent_git_commits(app_dir, limit)
            ref = "HEAD"
        return branch, ref, commits

    branch, ref, commits = await asyncio.to_thread(_get_commits)
    return {"branch": branch, "ref": ref, "commits": commits}


@router.get("/{app_id}/branches")
async def list_branches(app_id: int, db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("apps.view"))):
    """Branches of the app's repository, for the auto-deploy branch picker."""
    app = await _get_or_404(app_id, db)
    token = _decrypt_github_token(app.github_token)
    ok, msg, default_branch, branches = await asyncio.to_thread(_ls_remote, app.repo_url, token)
    current = await asyncio.to_thread(_current_branch, app.working_dir) if app.working_dir and os.path.exists(app.working_dir) else None
    return {"ok": ok, "message": msg, "branches": branches, "default_branch": default_branch, "current_branch": current}
