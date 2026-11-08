"""Pointing an app at another repository: the database and the git folder's `origin` change together (apps/git.py)."""
import asyncio
import os
import subprocess

import pytest


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _app_started(admin):
    """The database exists once the app has started."""


@pytest.fixture
def remote(monkeypatch):
    """What `git ls-remote` finds at the new address: the test decides."""
    from apps import git
    answer = {"ok": True, "message": "", "default": "main", "branches": ["main", "develop"], "asked": []}

    def fake(url, token):
        answer["asked"].append((url, token))
        return answer["ok"], answer["message"], answer["default"], answer["branches"]
    monkeypatch.setattr(git, "_ls_remote", fake)
    return answer


def _git(directory, *args):
    return subprocess.run(["git", "-C", str(directory), *args], capture_output=True, text=True, check=True).stdout.strip()


def _app(name, repo_url="https://github.com/old/repo", with_clone=True, branch="main", **fields):
    from containers import processes as pm
    from core.database import AsyncSessionLocal
    from core.models import Application

    async def go():
        async with AsyncSessionLocal() as db:
            app = Application(name=name, repo_url=repo_url, start_command="x", port=3000, status="stopped", **fields)
            db.add(app)
            await db.commit()
            return app.id
    app_id = _run(go())
    directory = pm.get_app_dir(app_id)
    if with_clone:
        os.makedirs(directory, exist_ok=True)
        _git(directory, "init", "-q", "-b", branch)
        _git(directory, "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "first")
        _git(directory, "remote", "add", "origin", repo_url)
    return app_id, directory


def _row(app_id):
    from core.database import AsyncSessionLocal
    from core.models import Application

    async def go():
        async with AsyncSessionLocal() as db:
            return await db.get(Application, app_id)
    return _run(go())


def test_the_database_and_the_origin_of_the_clone_both_change(admin, remote):
    app_id, directory = _app("repo-both")
    r = admin.put(f"/api/apps/{app_id}", json={"repo_url": "github.com/new/place/tree/main"})
    assert r.status_code == 200 and r.json()["repo_url"] == "https://github.com/new/place"      # cleaned up like at creation
    assert _row(app_id).repo_url == "https://github.com/new/place"
    assert _git(directory, "remote", "get-url", "origin") == "https://github.com/new/place"      # a pull fetches from here


def test_the_token_of_the_app_is_used_to_reach_the_new_repository_and_ends_up_in_origin(admin, remote):
    from apps.git import _encrypt_github_token
    app_id, directory = _app("repo-token", github_token=_encrypt_github_token("s3cret"))
    assert admin.put(f"/api/apps/{app_id}", json={"repo_url": "https://github.com/new/private"}).status_code == 200
    assert remote["asked"] == [("https://github.com/new/private", "s3cret")]
    assert _git(directory, "remote", "get-url", "origin") == "https://s3cret@github.com/new/private"


def test_a_repository_that_cannot_be_reached_changes_nothing(admin, remote):
    remote.update(ok=False, message="Repository not found. Check the URL.")
    app_id, directory = _app("repo-missing")
    r = admin.put(f"/api/apps/{app_id}", json={"repo_url": "https://github.com/new/nothing"})
    assert r.status_code == 400 and "not found" in r.json()["detail"].lower()
    assert _row(app_id).repo_url == "https://github.com/old/repo"
    assert _git(directory, "remote", "get-url", "origin") == "https://github.com/old/repo"


def test_the_branch_the_app_follows_must_exist_in_the_new_repository(admin, remote):
    app_id, directory = _app("repo-branch", deploy_branch="release")
    r = admin.put(f"/api/apps/{app_id}", json={"repo_url": "https://github.com/new/place"})
    assert r.status_code == 400 and "release" in r.json()["detail"]
    assert _row(app_id).repo_url == "https://github.com/old/repo"


def test_an_app_on_the_checked_out_branch_moves_to_the_default_branch_when_that_branch_is_gone(admin, remote):
    app_id, directory = _app("repo-default", branch="master")
    assert admin.put(f"/api/apps/{app_id}", json={"repo_url": "https://github.com/new/place"}).status_code == 200
    assert _row(app_id).deploy_branch == "main"                              # the new repository has no master


def test_the_same_address_is_not_looked_up_again(admin, remote):
    app_id, _ = _app("repo-same")
    assert admin.put(f"/api/apps/{app_id}", json={"repo_url": "https://github.com/old/repo/"}).status_code == 200
    assert remote["asked"] == []


def test_an_app_without_a_clone_yet_just_gets_the_new_address(admin, remote):
    app_id, _ = _app("repo-noclone", with_clone=False)
    assert admin.put(f"/api/apps/{app_id}", json={"repo_url": "https://github.com/new/place"}).status_code == 200
    assert _row(app_id).repo_url == "https://github.com/new/place"
