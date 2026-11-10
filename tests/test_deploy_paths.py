"""Every deploy, rebuild and rollback goes through apps/deployments.run_deploy: one lock, one history, one notification.
`pull=False` (Rebuild, and the Rolling / Blue-green buttons) uses the source as it is instead of fetching a commit."""
import asyncio
import tempfile

import pytest


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def deploy_env(client, monkeypatch, tmp_path):
    from sqlalchemy import delete, select
    from apps import deployments
    from core.database import AsyncSessionLocal
    from core.models import Application, Deployment
    calls = {"sync": 0, "build": 0, "replace": [], "notified": []}

    async def fake_sync(app, app_dir, commit, push, branch=None):
        calls["sync"] += 1
        return "abc1234 new commit", "rev-new"
    monkeypatch.setattr(deployments.apps_git, "_sync_app_source", fake_sync)
    monkeypatch.setattr(deployments.apps_git, "_refresh_app_source_revision", lambda app: "rev-current")
    monkeypatch.setattr(deployments.apps_git, "_current_branch", lambda d: "main")
    monkeypatch.setattr(deployments, "_commit_details", lambda d: ("abc1234deadbeef", "a commit", "dev"))

    def fake_build(*a, **k):
        calls["build"] += 1
        return "img", True
    monkeypatch.setattr(deployments.containers_build, "build_image_unless_current", fake_build)

    async def fake_replace(app_id, db, actor, *, batch):
        calls["replace"].append(batch)
        return {"replaced": 2, "instance_id": 99}
    monkeypatch.setattr(deployments.apps_rollout, "replace_instances", fake_replace)

    async def fake_notify(app_id, name, trigger, commit, error):
        calls["notified"].append(error)
    monkeypatch.setattr(deployments, "_notify", fake_notify)
    monkeypatch.setattr(deployments.apps_nginx_sync, "_has_public_nginx_domain", lambda app: True)
    made = []

    def make_app(name):
        async def go():
            async with AsyncSessionLocal() as db:
                app = Application(name=name, repo_url="https://example.test/x", start_command="x", port=3000,
                                  working_dir=tempfile.gettempdir())
                db.add(app)
                await db.commit()
                return app.id
        made.append(_run(go()))
        return made[-1]

    def history(app_id):
        async def go():
            async with AsyncSessionLocal() as db:
                return (await db.execute(select(Deployment).where(Deployment.app_id == app_id))).scalars().all()
        return _run(go())

    yield calls, make_app, history

    async def cleanup():
        async with AsyncSessionLocal() as db:
            await db.execute(delete(Deployment).where(Deployment.app_id.in_(made)))
            await db.execute(delete(Application).where(Application.id.in_(made)))
            await db.commit()
    _run(cleanup())


def test_rebuild_builds_the_source_as_it_is_and_is_in_the_history(deploy_env):
    from apps import deployments
    calls, make_app, history = deploy_env
    app_id = make_app("dp-rebuild")
    res = _run(deployments.run_deploy(app_id, strategy="rebuild", pull=False, actor="t"))
    assert calls["sync"] == 0 and calls["build"] == 1                      # no commit fetched, one build
    assert res["message"].startswith("Built abc1234")
    rows = history(app_id)
    assert [(d.strategy, d.status, d.commit_sha) for d in rows] == [("rebuild", "success", "abc1234deadbeef")]
    assert calls["notified"] == [None]


def test_pull_and_build_fetches_first(deploy_env):
    from apps import deployments
    calls, make_app, _ = deploy_env
    app_id = make_app("dp-pull")
    _run(deployments.run_deploy(app_id, strategy="rebuild", actor="t"))
    assert calls["sync"] == 1 and calls["build"] == 1


@pytest.mark.parametrize("path,batch", [("deploy-rolling", 1), ("deploy-blue-green", None)])
def test_the_rolling_and_blue_green_buttons_go_through_the_same_deploy(admin, deploy_env, path, batch):
    calls, make_app, history = deploy_env
    app_id = make_app(f"dp-{path}")
    r = admin.post(f"/api/apps/{app_id}/{path}")
    assert r.status_code == 200, r.text
    assert r.json()["replaced"] == 2 and r.json()["instance_id"] == 99          # what the buttons show
    assert calls["replace"] == [batch] and calls["sync"] == 0
    assert [d.status for d in history(app_id)] == ["success"]


def test_a_deploy_while_another_runs_is_refused(admin, deploy_env):
    from apps import state
    _, make_app, history = deploy_env
    app_id = make_app("dp-busy")
    with state.app_lock(app_id, "deploying"):
        assert admin.post(f"/api/apps/{app_id}/deploy-rolling").status_code == 409
    assert history(app_id) == []
