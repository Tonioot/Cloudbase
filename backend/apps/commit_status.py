"""Tell GitHub how a deploy went: the commit gets a status (pending, then success or failure) with a link to the deployment.

Uses the GitHub token that belongs to the app. Best effort: a missing token, a repository on another host or a
GitHub that does not answer never breaks a deploy."""
import asyncio
import json
import logging
import re
import urllib.request
from typing import Optional

from apps import git as apps_git
from core import system_config as _syscfg

log = logging.getLogger("cloudbase.deploys")

_REPO_RE = re.compile(r"^https://(?:www\.)?github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", re.IGNORECASE)
_STATES = {"pending", "success", "failure", "error"}


def github_repo(repo_url: Optional[str]) -> Optional[tuple[str, str]]:
    """(owner, name) for a github.com repository URL, otherwise None."""
    m = _REPO_RE.match((repo_url or "").strip())
    return (m.group(1), m.group(2)) if m else None


def target_url(app_id: int) -> Optional[str]:
    domain = _syscfg.get_panel_https_domain()
    return f"https://{domain}/app?id={app_id}" if domain else None


def _post(owner: str, name: str, sha: str, token: str, body: dict) -> None:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{name}/statuses/{sha}",
        data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json", "User-Agent": "Cloudbase"},
    )
    with urllib.request.urlopen(req, timeout=10):
        pass


async def report(app_id: int, repo_url: Optional[str], stored_token: Optional[str], sha: Optional[str],
                 state: str, description: str) -> None:
    """Set the status of `sha`. Never raises."""
    repo = github_repo(repo_url)
    token = apps_git._decrypt_github_token(stored_token)
    if not repo or not token or not sha or state not in _STATES:
        return
    body = {"state": state, "context": "cloudbase/deploy", "description": description[:140]}
    url = target_url(app_id)
    if url:
        body["target_url"] = url
    try:
        await asyncio.to_thread(_post, repo[0], repo[1], sha, token, body)
    except Exception as exc:
        log.debug("could not set the commit status of %s: %s", sha[:7], exc)
