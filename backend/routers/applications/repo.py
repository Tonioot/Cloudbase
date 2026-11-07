import asyncio
import logging
import os
import re
from fastapi import HTTPException, Query, Depends, APIRouter
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import get_db
from containers import processes as pm
from containers import dockerfiles as containers_dockerfiles
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


# Never part of what a node builds from
_NOT_SENT = {".git", "__pycache__", "node_modules", ".venv", "venv"}


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _pack_source(source_dir: str) -> tuple[str, list[str]]:
    """The working directory as a .tar.gz in a temporary file (not in memory: the source can be hundreds of MB).

    A file that cannot be read (a leftover owned by someone else, one that vanished) is left out and reported,
    instead of failing the whole download. Returns (path of the archive, the files left out)."""
    import tarfile
    import tempfile
    skipped: list[str] = []
    fd, path = tempfile.mkstemp(prefix="cb-source-", suffix=".tar.gz")
    try:
        with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tar:
            for directory, dirs, files in os.walk(source_dir):
                dirs[:] = [d for d in dirs if d not in _NOT_SENT]
                for name in [""] + files:                  # the directory itself, then its files
                    full = os.path.join(directory, name) if name else directory
                    if name in _NOT_SENT:
                        continue
                    arcname = os.path.relpath(full, source_dir)
                    try:
                        tar.add(full, arcname=("." if arcname == "." else arcname), recursive=False)
                    except OSError as exc:
                        skipped.append(f"{arcname} ({exc.__class__.__name__})")
    except BaseException:
        _remove_quietly(path)
        raise
    return path, skipped


@router.get("/{app_id}/source-archive")
async def get_source_archive(app_id: int, db: AsyncSession = Depends(get_db), _node: None = Depends(_source_archive_auth)):
    """Return a .tar.gz of the app's working directory for remote nodes to build from.
    Only the local primary serves this — remote nodes call this endpoint directly."""
    app = await _get_or_404(app_id, db)
    source_dir = pm.app_source_dir(app)
    if not source_dir or not os.path.exists(source_dir):
        raise HTTPException(404, "App source directory not found — deploy the app first")

    # Refresh the generated Dockerfile first so the node receives the current
    # one (with marker). Nodes get no .git, so they treat an unmarked
    # Dockerfile as the repository's own.
    try:
        await asyncio.to_thread(
            containers_dockerfiles.ensure_dockerfile,
            source_dir,
            app.app_type or "unknown",
            app.start_command or "",
            _image_port(app),
            **_dockerfile_build_kwargs(app),
        )
    except Exception as exc:
        log.warning("[source-archive] Could not refresh Dockerfile for app=%s: %s", app.name, exc)

    try:
        path, skipped = await asyncio.to_thread(_pack_source, source_dir)
    except Exception as exc:
        log.error("[source-archive] Could not pack the source of app=%s: %s", app.name, exc, exc_info=True)
        raise HTTPException(500, f"Could not pack the source of '{app.name}': {type(exc).__name__}: {exc}") from exc
    if skipped:
        log.warning("[source-archive] app=%s: left out %d file(s) that could not be read, e.g. %s", app.name, len(skipped), skipped[0])
    return FileResponse(
        path, media_type="application/gzip", filename=f"{app.name}-source.tar.gz",
        background=BackgroundTask(_remove_quietly, path),
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
