import asyncio
import logging
import os
import re
import shutil
import subprocess
from typing import Optional
from fastapi import HTTPException
from core.models import Application
from containers import processes as pm
from containers import dockerfiles as containers_dockerfiles
from core import token_vault
from core.env_crypto import decrypt_text, encrypt_text
from apps.images import _dockerfile_build_kwargs, _image_port

log = logging.getLogger("cloudbase.apps")


def _resolve_token(req_token: Optional[str], req_token_id: Optional[str]) -> Optional[str]:
    """Return raw token: prefer vault lookup, fall back to inline value."""
    if req_token_id:
        resolved = token_vault.resolve(req_token_id)
        if resolved:
            return resolved
    return req_token or None


def _encrypt_github_token(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    return encrypt_text(token)


def _github_token_label(app) -> Optional[str]:
    """Return a display label for the app's GitHub token — never the raw value.
    If the token came from the vault, return its label. Otherwise return '****'."""
    if not app.github_token:
        return None
    if app.github_token_id:
        hints = token_vault.list_hints()
        match = next((h for h in hints if h["id"] == app.github_token_id), None)
        if match:
            return match["label"]
    return "****"


def _decrypt_github_token(stored: Optional[str]) -> Optional[str]:
    if not stored:
        return None
    plain = decrypt_text(stored, fallback_plaintext=True)
    return plain or None


def _build_clone_url(repo_url: str, token: Optional[str]) -> str:
    """Put an access token in an HTTPS clone URL, in the form each host expects."""
    if not token or not repo_url.startswith("https://"):
        return repo_url
    from urllib.parse import quote
    host = repo_url[len("https://"):].split("/")[0].lower()
    if "@" in host:
        return repo_url  # credentials already in the URL
    if host == "github.com" or host.endswith(".github.com"):
        userinfo = quote(token, safe="")
    elif "gitlab" in host:
        userinfo = f"oauth2:{quote(token, safe='')}"
    elif host == "bitbucket.org":
        userinfo = f"x-token-auth:{quote(token, safe='')}"
    else:  # Gitea, Forgejo, Codeberg and most others accept the token as password
        userinfo = f"git:{quote(token, safe='')}"
    return repo_url.replace("https://", f"https://{userinfo}@", 1)


def _normalize_repo_url(value: str) -> str:
    """Accept what people paste: github.com/user/repo, a browser URL with
    /tree/main, a trailing slash or .git."""
    url = (value or "").strip()
    if url and "://" not in url and not url.startswith("git@"):
        url = "https://" + url
    url = re.sub(r"/(tree|blob)/.*$", "", url).rstrip("/")
    return url


def _ls_remote(url: str, token: Optional[str]) -> tuple[bool, str, Optional[str], list[str]]:
    """(ok, message, default_branch, branches) — talks to the remote without cloning."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo"}
    try:
        r = subprocess.run(
            ["git", "ls-remote", "--symref", _build_clone_url(url, token), "HEAD", "refs/heads/*"],
            capture_output=True, text=True, timeout=20, env=env,
        )
    except subprocess.TimeoutExpired:
        return False, "The repository didn’t answer within 20 seconds. Check the URL.", None, []
    except FileNotFoundError:
        return False, "git isn’t installed on the server.", None, []
    if r.returncode != 0:
        low = (r.stderr or "").lower()
        if "could not read username" in low or "authentication failed" in low or "terminal prompts disabled" in low:
            msg = ("This repository is private or doesn’t exist. Add a token with read access."
                   if not token else "The token doesn’t give access to this repository, or the repository doesn’t exist.")
        elif "not found" in low or "does not appear to be a git repository" in low:
            msg = "Repository not found. Check the URL — and the token if the repository is private."
        elif "could not resolve host" in low:
            msg = "That host doesn’t exist. Check the URL."
        else:
            msg = (r.stderr or "Couldn’t read the repository.").strip().splitlines()[-1]
        return False, msg, None, []
    default_branch, branches = None, []
    for line in r.stdout.splitlines():
        if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD"):
            default_branch = line[len("ref: refs/heads/"):].split("\t")[0]
        elif "\trefs/heads/" in line:
            branches.append(line.split("\trefs/heads/", 1)[1])
    if not branches:
        return False, "The repository is empty — push a commit first.", None, []
    return True, "", default_branch or branches[0], sorted(branches)


def _friendly_git_clone_error(stderr: str) -> str:
    raw = (stderr or "").strip()
    low = raw.lower()

    if "could not read username for" in low or "authentication failed" in low:
        return "Git clone failed: authentication required for this repository. Add a valid access token and try again."

    if "repository not found" in low and "github.com" in low:
        return "Git clone failed: repository not found or access denied. Check the repo URL and the token’s permissions."

    if "permission denied" in low:
        return "Git clone failed: permission denied. Verify repository access and deploy credentials."

    if raw:
        return f"Git clone failed: {raw}"
    return "Git clone failed for an unknown reason."


def _git_app_dir_or_404(app_name: str) -> str:
    app_dir = pm.get_app_dir(app_name)
    if not os.path.exists(app_dir):
        raise HTTPException(404, "App directory does not exist on disk")
    if not os.path.exists(os.path.join(app_dir, ".git")):
        raise HTTPException(
            400,
            "Repository is not initialized for this app. Deploy it successfully first.",
        )
    return app_dir


def _fetch_origin(app_dir: str, branch: str) -> None:
    fetch = subprocess.run(["git", "fetch", "origin", branch], cwd=app_dir, capture_output=True, text=True)
    if fetch.returncode != 0:
        fetch = subprocess.run(["git", "fetch", "origin"], cwd=app_dir, capture_output=True, text=True)
    if fetch.returncode != 0:
        raise HTTPException(500, f"Git fetch failed: {fetch.stderr}")


def _current_branch(app_dir: str) -> str:
    br_res = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=app_dir, capture_output=True, text=True)
    return br_res.stdout.strip() if br_res.returncode == 0 else "main"


def _git_head_commit(app_dir: str) -> Optional[str]:
    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=app_dir, capture_output=True, text=True)
    if res.returncode != 0:
        return None
    value = (res.stdout or "").strip()
    return value or None


def _git_worktree_dirty(app_dir: str) -> bool:
    # Dockerfile and .dockerignore are written by Cloudbase during the build;
    # counting them would flip the revision to "-dirty" after every build and
    # trigger a second, redundant build on the next start.
    res = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--",
         ".", ":(exclude)Dockerfile", ":(exclude).dockerignore"],
        cwd=app_dir,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        return False
    return bool((res.stdout or "").strip())


def _resolve_source_revision(app_dir: str) -> Optional[str]:
    head = _git_head_commit(app_dir)
    if not head:
        return None
    if _git_worktree_dirty(app_dir):
        return f"{head}-dirty"
    return head


def _refresh_app_source_revision(app: Application) -> Optional[str]:
    app_dir = app.working_dir
    if not app_dir or not os.path.exists(os.path.join(app_dir, ".git")):
        return app.source_revision
    revision = _resolve_source_revision(app_dir)
    if revision:
        app.source_revision = revision
    return revision


def _recent_git_commits(app_dir: str, limit: int = 20, ref: Optional[str] = None) -> list[dict]:
    fmt = "%H%x1f%h%x1f%s%x1f%cr%x1f%an"
    cmd = ["git", "log"]
    if ref:
        cmd.append(ref)
    cmd.extend([f"-n{limit}", f"--format={fmt}", "--decorate"])
    res = subprocess.run(
        cmd,
        cwd=app_dir,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        raise HTTPException(500, f"Git log failed: {res.stderr}")

    commits = []
    for line in res.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 5:
            continue
        full_hash, short_hash, subject, relative_time, author = parts
        commits.append({
            "hash": full_hash,
            "short_hash": short_hash,
            "subject": subject,
            "relative_time": relative_time,
            "author": author,
        })
    return commits


async def _deploy_app(app: Application):
    app_dir = pm.get_app_dir(app.name)
    log.info("[deploy] Starting deployment for app=%s in dir=%s", app.name, app_dir)

    # local://primary means source was already extracted by the node agent — skip clone
    if app.repo_url == "local://primary":
        if not os.path.exists(app_dir):
            raise HTTPException(500, "Source directory missing for local app — re-deploy from primary")
        app.working_dir = app.working_dir or app_dir
        app.source_revision = _resolve_source_revision(app_dir)
        app.image_revision = None
    else:
        # If directory exists but is broken or empty, clean it up
        if os.path.exists(app_dir):
            if not os.path.exists(os.path.join(app_dir, ".git")):
                log.warning("[deploy] Directory %s exists but is not a git repo, cleaning up for fresh clone", app_dir)
                shutil.rmtree(app_dir)
                os.makedirs(app_dir, exist_ok=True)
        else:
            os.makedirs(app_dir, exist_ok=True)

        github_token = _decrypt_github_token(app.github_token)
        clone_url = _build_clone_url(app.repo_url, github_token)
        log.info("[deploy] Cloning from %s", app.repo_url.replace(github_token or "SECRET", "***") if github_token else app.repo_url)

        result = await asyncio.to_thread(subprocess.run,
            ["git", "clone", "--", clone_url, "."],
            cwd=app_dir,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            log.error("[deploy] Git clone failed: %s", result.stderr)
            raise HTTPException(400, _friendly_git_clone_error(result.stderr))

        app.working_dir = app_dir
        app.source_revision = _resolve_source_revision(app_dir)
        app.image_revision = None
    if app.app_type != "static":
        app.app_type = pm.detect_app_type_from_command(app.start_command) if app.start_command else "unknown"

    containers_dockerfiles.ensure_dockerfile(
        app_dir,
        app.app_type or "unknown",
        app.start_command or "",
        _image_port(app),
        **_dockerfile_build_kwargs(app),
    )
    log.info("[deploy] Deployment finished for app=%s", app.name)


async def _sync_app_source(app: Application, app_dir: str, target_commit: Optional[str], log, branch: Optional[str] = None) -> tuple[str, Optional[str]]:
    """Fetch and hard-reset the app's working tree to target_commit (or the
    branch head). With `branch`, switch to that branch first.
    Updates app.source_revision; returns (commit_info, revision)."""
    github_token = _decrypt_github_token(app.github_token)
    if github_token:
        url = _build_clone_url(app.repo_url, github_token)
        await asyncio.to_thread(subprocess.run, ["git", "remote", "set-url", "origin", url], cwd=app_dir, capture_output=True)

    current = _current_branch(app_dir)
    branch = branch or current
    log(f"[Git] Fetching from origin ({branch})…")
    await asyncio.to_thread(_fetch_origin, app_dir, branch)
    if branch != current:
        log(f"[Git] Switching branch {current} → {branch}…")
        co = await asyncio.to_thread(
            subprocess.run, ["git", "checkout", "-B", branch, f"origin/{branch}"], cwd=app_dir, capture_output=True, text=True,
        )
        if co.returncode != 0:
            raise HTTPException(500, f"Git checkout of {branch} failed: {co.stderr.strip()}")

    target = target_commit or f"origin/{branch}"
    log(f"[Git] Resetting to {target}…")
    reset = await asyncio.to_thread(
        subprocess.run, ["git", "reset", "--hard", target], cwd=app_dir, capture_output=True, text=True,
    )
    if reset.returncode != 0 and not target_commit:
        reset = await asyncio.to_thread(
            subprocess.run, ["git", "reset", "--hard", "@{u}"], cwd=app_dir, capture_output=True, text=True,
        )
    if reset.returncode != 0:
        raise HTTPException(500, f"Git reset failed: {reset.stderr}")

    log_res = await asyncio.to_thread(
        subprocess.run, ["git", "log", "-1", "--format=%h - %s (%cr)"], cwd=app_dir, capture_output=True, text=True,
    )
    commit_info = log_res.stdout.strip() if log_res.returncode == 0 else "Unknown"
    log(f"[Git] Updated to: {commit_info}")
    source_revision = _resolve_source_revision(app_dir)
    if source_revision:
        app.source_revision = source_revision
    return commit_info, source_revision
