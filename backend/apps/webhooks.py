"""Git webhooks: a push to the branch an app follows deploys it at once.

Each app can have a secret. The Git host (GitHub, GitLab, Gitea) signs every delivery with it, and only a delivery
with a valid signature starts a deploy. The polling of `workers/autodeploy.py` stays as the fallback."""
import asyncio
import hashlib
import hmac
import json
import logging
import secrets
from typing import Optional

from fastapi import HTTPException

from core.env_crypto import decrypt_text, encrypt_text

log = logging.getLogger("cloudbase.deploys")

_ZERO_SHA = "0" * 40


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def store(secret: str) -> str:
    return encrypt_text(secret)


def secret_of(app) -> Optional[str]:
    return (decrypt_text(app.webhook_secret) or None) if app.webhook_secret else None


def verify(headers, body: bytes, secret: str) -> Optional[str]:
    """Which Git host sent this, if the signature or token is right: "github", "gitea" or "gitlab"; otherwise None."""
    def mac() -> str:
        return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    sig = headers.get("x-hub-signature-256")
    if sig is not None:
        ok = hmac.compare_digest(sig.strip().removeprefix("sha256=").lower(), mac())
        return ("gitea" if headers.get("x-gitea-event") else "github") if ok else None
    sig = headers.get("x-gitea-signature")
    if sig is not None:
        return "gitea" if hmac.compare_digest(sig.strip().lower(), mac()) else None
    token = headers.get("x-gitlab-token")
    if token is not None:
        return "gitlab" if hmac.compare_digest(token.encode(), secret.encode()) else None
    return None


def parse_push(provider: str, headers, body: bytes) -> tuple[str, Optional[tuple[str, str]]]:
    """(kind, (branch, sha)): kind is "ping", "push" or "ignored"; the pair only for a push to a branch."""
    event = (headers.get("x-github-event") or headers.get("x-gitea-event") or headers.get("x-gitlab-event") or "").lower()
    if event == "ping":
        return "ping", None
    if event not in ("push", "push hook"):
        return "ignored", None
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(400, "The delivery is not JSON")
    ref = str(data.get("ref") or "")
    sha = str(data.get("after") or data.get("checkout_sha") or "")
    if not ref.startswith("refs/heads/") or not sha or sha == _ZERO_SHA:
        return "ignored", None   # a tag, or a branch that was deleted
    return "push", (ref[len("refs/heads/"):], sha)


# One deploy per app at a time; a push that comes in meanwhile is deployed right after (only the newest counts).
_latest: dict[int, str] = {}
_running: set[int] = set()


async def deploy_pushed(app_id: int, sha: str, run_deploy, strategy: str, *, retry_seconds: float = 30, tries: int = 20) -> None:
    _latest[app_id] = sha
    if app_id in _running:
        return
    _running.add(app_id)
    try:
        while (wanted := _latest.pop(app_id, None)):
            for _ in range(tries):
                try:
                    await run_deploy(app_id, commit=wanted, strategy=strategy, trigger="webhook", actor="webhook")
                    break
                except HTTPException as exc:
                    if exc.status_code != 409:
                        log.warning("[webhook] app %s: deploy of %s failed: %s", app_id, wanted[:7], exc.detail)
                        break
                    await asyncio.sleep(retry_seconds)   # busy with another action: try again soon
    finally:
        _running.discard(app_id)
