"""A push webhook starts a deploy; only a delivery with a valid signature may."""
import asyncio
import hashlib
import hmac
import json
import os

import pytest
from fastapi import HTTPException

SHA = "a" * 40


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _app_started(admin):
    """The database exists once the app has started."""


def _app(name, tmp_path, **fields):
    from core.database import AsyncSessionLocal
    from core.models import Application

    async def go():
        async with AsyncSessionLocal() as db:
            app = Application(name=name, repo_url="https://github.com/me/site", start_command="x", port=3000,
                              working_dir=str(tmp_path), **fields)
            db.add(app)
            await db.commit()
            return app.id
    return _run(go())


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _push(branch="main", sha=SHA):
    return json.dumps({"ref": f"refs/heads/{branch}", "after": sha}).encode()


# ── signatures ────────────────────────────────────────────────────────────────

def test_github_gitea_and_gitlab_deliveries_are_recognised_by_their_signature():
    from apps import webhooks
    body = b"{}"
    assert webhooks.verify({"x-hub-signature-256": _sign("s", body)}, body, "s") == "github"
    assert webhooks.verify({"x-hub-signature-256": _sign("s", body), "x-gitea-event": "push"}, body, "s") == "gitea"
    assert webhooks.verify({"x-gitea-signature": _sign("s", body)[7:]}, body, "s") == "gitea"
    assert webhooks.verify({"x-gitlab-token": "s"}, body, "s") == "gitlab"


def test_a_wrong_or_missing_signature_is_refused():
    from apps import webhooks
    body = b"{}"
    assert webhooks.verify({"x-hub-signature-256": _sign("other", body)}, body, "s") is None
    assert webhooks.verify({"x-hub-signature-256": _sign("s", b"changed")}, body, "s") is None
    assert webhooks.verify({"x-gitlab-token": "nope"}, body, "s") is None
    assert webhooks.verify({}, body, "s") is None


def test_only_a_push_to_a_branch_counts():
    from apps import webhooks
    assert webhooks.parse_push("github", {"x-github-event": "ping"}, b"{}")[0] == "ping"
    assert webhooks.parse_push("github", {"x-github-event": "issues"}, b"{}")[0] == "ignored"
    assert webhooks.parse_push("github", {"x-github-event": "push"}, _push("dev")) == ("push", ("dev", SHA))
    assert webhooks.parse_push("gitlab", {"x-gitlab-event": "Push Hook"}, _push()) == ("push", ("main", SHA))
    tag = json.dumps({"ref": "refs/tags/v1", "after": SHA}).encode()
    assert webhooks.parse_push("github", {"x-github-event": "push"}, tag)[0] == "ignored"
    deleted = _push(sha="0" * 40)
    assert webhooks.parse_push("github", {"x-github-event": "push"}, deleted)[0] == "ignored"
    with pytest.raises(HTTPException):
        webhooks.parse_push("github", {"x-github-event": "push"}, b"not json")


# ── the endpoint ──────────────────────────────────────────────────────────────

def _switch_on(admin, app_id):
    r = admin.post(f"/api/apps/{app_id}/webhook")
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] and r.json()["path"] == f"/api/webhooks/{app_id}"
    return r.json()["secret"]


def _deliver(client, app_id, secret, body, event="push", sign=True):
    headers = {"X-GitHub-Event": event}
    if sign:
        headers["X-Hub-Signature-256"] = _sign(secret, body)
    return client.post(f"/api/webhooks/{app_id}", content=body, headers=headers)


def test_the_webhook_can_be_switched_on_renewed_and_off(admin, tmp_path):
    app_id = _app("hook-settings", tmp_path)
    assert admin.get(f"/api/apps/{app_id}/webhook").json() == {"enabled": False, "path": f"/api/webhooks/{app_id}", "secret": None}
    first = _switch_on(admin, app_id)
    second = admin.post(f"/api/apps/{app_id}/webhook").json()["secret"]
    assert first != second
    assert admin.get(f"/api/apps/{app_id}/webhook").json()["secret"] == second
    assert admin.delete(f"/api/apps/{app_id}/webhook").json()["enabled"] is False


def test_the_secret_is_stored_encrypted(admin, tmp_path):
    from core.database import AsyncSessionLocal
    from core.models import Application
    app_id = _app("hook-stored", tmp_path)
    secret = _switch_on(admin, app_id)

    async def stored():
        async with AsyncSessionLocal() as db:
            return (await db.get(Application, app_id)).webhook_secret
    assert secret not in _run(stored())


def test_a_delivery_without_a_valid_signature_is_refused(client, admin, tmp_path):
    app_id = _app("hook-refused", tmp_path)
    secret = _switch_on(admin, app_id)
    assert _deliver(client, app_id, secret, _push(), sign=False).status_code == 401
    assert _deliver(client, app_id, "wrong", _push()).status_code == 401
    assert _deliver(client, 999999, secret, _push()).status_code == 401           # unknown app: the same answer


def test_an_app_without_a_webhook_refuses_every_delivery(client, tmp_path):
    app_id = _app("hook-off", tmp_path)
    assert _deliver(client, app_id, "anything", _push()).status_code == 401


def test_a_ping_is_answered(client, admin, tmp_path):
    app_id = _app("hook-ping", tmp_path)
    secret = _switch_on(admin, app_id)
    r = _deliver(client, app_id, secret, b"{}", event="ping")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_a_push_to_the_followed_branch_deploys(client, admin, tmp_path, monkeypatch):
    from apps import webhooks
    app_id = _app("hook-deploy", tmp_path, auto_deploy=True, deploy_branch="main", deploy_strategy="blue_green")
    secret = _switch_on(admin, app_id)
    started = []

    async def fake(app_id, sha, run_deploy, strategy, **kw):
        started.append((app_id, sha, strategy))
    monkeypatch.setattr(webhooks, "deploy_pushed", fake)
    r = _deliver(client, app_id, secret, _push("main"))
    assert r.status_code == 202 and r.json() == {"status": "deploying", "commit": SHA}
    assert started == [(app_id, SHA, "blue_green")]


def test_a_push_to_another_branch_or_of_a_deployed_commit_does_nothing(client, admin, tmp_path, monkeypatch):
    from apps import webhooks
    app_id = _app("hook-skip", tmp_path, auto_deploy=True, deploy_branch="main", source_revision=SHA)
    secret = _switch_on(admin, app_id)
    started = []

    async def fake(*a, **kw):
        started.append(a)
    monkeypatch.setattr(webhooks, "deploy_pushed", fake)
    assert _deliver(client, app_id, secret, _push("dev")).json()["status"] == "ignored"
    assert _deliver(client, app_id, secret, _push("main", SHA)).json()["status"] == "ignored"   # already deployed
    assert not started


def test_a_push_does_nothing_while_auto_deploy_is_off(client, admin, tmp_path, monkeypatch):
    from apps import webhooks
    app_id = _app("hook-autooff", tmp_path, auto_deploy=False, deploy_branch="main")
    secret = _switch_on(admin, app_id)
    started = []

    async def fake(*a, **kw):
        started.append(a)
    monkeypatch.setattr(webhooks, "deploy_pushed", fake)
    r = _deliver(client, app_id, secret, _push("main"))
    assert r.status_code == 200 and r.json()["status"] == "ignored" and "auto-deploy" in r.json()["reason"]
    assert not started


def test_an_app_without_source_cannot_be_deployed_by_a_push(client, admin, tmp_path):
    missing = tmp_path / "nowhere"
    app_id = _app("hook-nosource", missing, auto_deploy=True, deploy_branch="main")
    assert not os.path.exists(missing)
    secret = _switch_on(admin, app_id)
    assert _deliver(client, app_id, secret, _push("main")).status_code == 409


# ── a push while another deploy runs ──────────────────────────────────────────

def test_pushes_during_a_deploy_are_deployed_after_it_and_only_the_newest():
    from apps import webhooks
    calls, gate = [], asyncio.Event()

    async def run_deploy(app_id, *, commit, strategy, trigger, actor):
        calls.append((commit, trigger, actor))
        await gate.wait()

    async def scenario():
        first = asyncio.create_task(webhooks.deploy_pushed(1, "one", run_deploy, "rolling"))
        await asyncio.sleep(0)
        await webhooks.deploy_pushed(1, "two", run_deploy, "rolling")      # arrives while "one" runs
        await webhooks.deploy_pushed(1, "three", run_deploy, "rolling")
        gate.set()
        await first
    _run(scenario())
    assert calls == [("one", "webhook", "webhook"), ("three", "webhook", "webhook")]


def test_a_busy_app_is_tried_again(monkeypatch):
    from apps import webhooks
    attempts = []

    async def run_deploy(app_id, **kw):
        attempts.append(1)
        if len(attempts) < 3:
            raise HTTPException(409, "busy")
    _run(webhooks.deploy_pushed(2, "x", run_deploy, "rolling", retry_seconds=0, tries=5))
    assert len(attempts) == 3


# ── commit status on GitHub ───────────────────────────────────────────────────

def test_only_github_repositories_get_a_commit_status():
    from apps import commit_status
    assert commit_status.github_repo("https://github.com/me/site.git") == ("me", "site")
    assert commit_status.github_repo("https://github.com/me/site") == ("me", "site")
    assert commit_status.github_repo("https://gitlab.com/me/site") is None
    assert commit_status.github_repo("git@github.com:me/site.git") is None


def test_the_commit_status_is_posted_with_the_apps_token(monkeypatch):
    from apps import commit_status
    from core.env_crypto import encrypt_text
    posted = []
    monkeypatch.setattr(commit_status, "_post", lambda *a: posted.append(a))
    _run(commit_status.report(7, "https://github.com/me/site", encrypt_text("tok"), SHA, "success", "Deployed"))
    owner, name, sha, token, body = posted[0]
    assert (owner, name, sha, token) == ("me", "site", SHA, "tok")
    assert body["state"] == "success" and body["context"] == "cloudbase/deploy"


def test_no_commit_status_without_a_token_or_for_another_host(monkeypatch):
    from apps import commit_status
    from core.env_crypto import encrypt_text
    posted = []
    monkeypatch.setattr(commit_status, "_post", lambda *a: posted.append(a))
    _run(commit_status.report(7, "https://github.com/me/site", None, SHA, "success", ""))
    _run(commit_status.report(7, "https://gitlab.com/me/site", encrypt_text("tok"), SHA, "success", ""))
    assert not posted


def test_a_failing_commit_status_never_breaks_a_deploy(monkeypatch):
    from apps import commit_status
    from core.env_crypto import encrypt_text

    def boom(*a):
        raise OSError("github is down")
    monkeypatch.setattr(commit_status, "_post", boom)
    _run(commit_status.report(7, "https://github.com/me/site", encrypt_text("tok"), SHA, "failure", "x"))
