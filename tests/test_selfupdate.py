"""Updating Cloudbase from inside Cloudbase (the "Update Cloudbase" button of a node), without root."""
import asyncio
import subprocess

import pytest

from core import selfupdate


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args], check=True, capture_output=True)


@pytest.fixture
def repos(tmp_path, monkeypatch):
    """A remote and a checkout of it, as an installation under systemd."""
    remote, local = tmp_path / "remote", tmp_path / "local"
    remote.mkdir()
    _git(remote, "init", "-b", "main")
    (remote / "VERSION").write_text("1.0.0")
    (remote / "app.py").write_text("one")
    _git(remote, "add", "-A")
    _git(remote, "commit", "-m", "one")
    subprocess.run(["git", "clone", str(remote), str(local)], check=True, capture_output=True)
    monkeypatch.setattr(selfupdate.paths, "REPO_DIR", str(local))
    monkeypatch.setattr(selfupdate.paths, "VERSION_FILE", str(local / "VERSION"))
    monkeypatch.setenv("INVOCATION_ID", "systemd")
    selfupdate.current.cache_clear()
    yield remote, local
    selfupdate.current.cache_clear()


def _push(remote, name, text):
    (remote / name).parent.mkdir(parents=True, exist_ok=True)
    (remote / name).write_text(text)
    _git(remote, "add", "-A")
    _git(remote, "commit", "-m", name)


def test_what_an_installation_runs_is_its_commit_and_version(repos):
    assert selfupdate.current()["version"] == "1.0.0" and len(selfupdate.current()["commit"]) >= 7


def test_nothing_new_is_reported_as_current(repos):
    assert selfupdate.update()["status"] == "current"


def test_a_new_version_is_applied(repos):
    remote, local = repos
    _push(remote, "app.py", "two")
    r = selfupdate.update()
    assert r["status"] == "updated" and r["from"] != r["commit"]
    assert (local / "app.py").read_text() == "two"


def test_a_version_that_changes_system_files_is_left_alone(repos):
    remote, local = repos
    _push(remote, "install.sh", "echo new")
    _push(remote, "app.py", "two")
    r = selfupdate.update()
    assert r["status"] == "needs_system" and "install.sh" in r["files"] and "cloudbase update" in r["message"]
    assert (local / "app.py").read_text() == "one"            # nothing was applied: never half updated
    _push(remote, "scripts/cloudbase-wireguard", "x")
    assert selfupdate.update()["status"] == "needs_system"


def test_files_changed_by_hand_are_not_overwritten(repos):
    remote, local = repos
    _push(remote, "app.py", "two")
    (local / "app.py").write_text("my edit")
    r = selfupdate.update()
    assert r["status"] == "error" and (local / "app.py").read_text() == "my edit"


def test_without_a_service_manager_it_cannot_restart_so_it_does_not_update(repos, monkeypatch):
    remote, local = repos
    _push(remote, "app.py", "two")
    monkeypatch.delenv("INVOCATION_ID")
    r = selfupdate.update()
    assert r["status"] == "error" and (local / "app.py").read_text() == "one"


def test_a_checkout_that_is_not_git_cannot_update(tmp_path, monkeypatch):
    monkeypatch.setattr(selfupdate.paths, "REPO_DIR", str(tmp_path))
    assert selfupdate.update()["status"] == "error"


def test_the_system_paths_are_recognised():
    assert selfupdate.needs_system(["backend/x.py", "install.sh", "scripts/setup-wireguard.sh", "scripts/dev.sh"]) == [
        "install.sh", "scripts/setup-wireguard.sh"]


# ── the button ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _app_started(admin):
    """The database exists once the app has started."""


def test_the_primary_updates_itself_and_restarts_only_after_a_real_update(admin, monkeypatch):
    restarts = []
    monkeypatch.setattr(selfupdate, "restart_soon", lambda graceful, delay=3.0: restarts.append(graceful))
    monkeypatch.setattr(selfupdate, "update", lambda: {"status": "current", "message": "Already up to date"})
    primary = next(n for n in admin.get("/api/nodes").json() if n["is_local"])
    assert admin.post(f"/api/nodes/{primary['id']}/update").json()["status"] == "current" and not restarts
    monkeypatch.setattr(selfupdate, "update", lambda: {"status": "updated", "commit": "abc1234"})
    assert admin.post(f"/api/nodes/{primary['id']}/update").json()["status"] == "updated"
    assert restarts == [True]                                  # the panel stops gracefully


def test_a_node_that_is_not_connected_is_skipped_and_an_old_agent_is_explained(admin, monkeypatch):
    from core.database import AsyncSessionLocal
    from core.models import Node
    from nodes import commands as node_commands, updates
    from nodes.state import _node_ws_connections

    async def make():
        async with AsyncSessionLocal() as db:
            n = Node(name="remote-update", auth_token="t-update", is_local=False, enabled=True, status="online")
            db.add(n)
            await db.commit()
            return n.id
    node_id = asyncio.run(make())
    assert admin.post(f"/api/nodes/{node_id}/update").json()["status"] == "skipped"

    class Done:
        status, error_message, result = "failed", "Unsupported command type: 'self_update'", None

    async def ask(*a, **kw):
        return Done()
    monkeypatch.setitem(_node_ws_connections, node_id, object())
    monkeypatch.setattr(node_commands, "ask", ask)
    r = admin.post(f"/api/nodes/{node_id}/update").json()
    assert r["status"] == "error" and "cloudbase update" in r["message"] and updates._OLD_AGENT == r["message"]


def test_a_node_reports_what_it_runs():
    from agent.registration import _build_capabilities
    caps = _build_capabilities()
    assert "code_commit" in caps and "code_version" in caps
