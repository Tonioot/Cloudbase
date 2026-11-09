"""One action at a time per app (apps/state.py): whoever changes an app's instances holds the app, a second
one is refused with a 409 instead of racing the first."""
import asyncio
import tempfile

import pytest
from fastapi import HTTPException

from apps import state


@pytest.fixture(autouse=True)
def _clean():
    state._busy.clear()
    yield
    state._busy.clear()


def test_a_second_claim_is_refused_and_names_what_is_going_on():
    with state.app_lock(1, "deploying"):
        assert state.busy_with(1) == "deploying"
        with pytest.raises(HTTPException) as err:
            with state.app_lock(1, "scaling"):
                pass
        assert err.value.status_code == 409 and "deploying" in err.value.detail
        assert state.busy_with(1) == "deploying"            # the refused claim did not take it over
    assert state.busy_with(1) is None


def test_other_apps_are_not_affected():
    with state.app_lock(1, "deploying"):
        with state.app_lock(2, "scaling"):
            assert state.busy_with(2) == "scaling"


def test_the_app_is_released_when_the_action_fails():
    with pytest.raises(RuntimeError):
        with state.app_lock(1, "deploying"):
            raise RuntimeError("boom")
    assert state.busy_with(1) is None
    with state.app_lock(1, "stopping"):                      # and can be claimed again
        pass


def test_the_refused_claim_does_not_release_the_holder():
    state.acquire_app(1, "deploying")
    with pytest.raises(HTTPException):
        state.acquire_app(1, "scaling")
    state.release_app(1)
    assert state.busy_with(1) is None


def test_decorator_takes_the_app_id_positionally_or_by_name():
    seen = []

    @state.locked("stopping")
    async def act(app_id, other=None):
        seen.append(state.busy_with(app_id))

    async def go():
        await act(5)
        await act(app_id=6, other=1)
    asyncio.run(go())
    assert seen == ["stopping", "stopping"]
    assert state.busy_with(5) is None and state.busy_with(6) is None


def test_two_actions_at_once_only_one_runs():
    """The second starts while the first is waiting: it is refused, the first finishes undisturbed."""
    log = []

    @state.locked("scaling")
    async def slow(app_id, gate):
        log.append("start")
        await gate.wait()
        log.append("end")

    async def go():
        gate = asyncio.Event()
        first = asyncio.create_task(slow(1, gate))
        await asyncio.sleep(0)                               # the first is now inside, waiting
        with pytest.raises(HTTPException) as err:
            await slow(1, gate)
        assert err.value.status_code == 409
        gate.set()
        await first
    asyncio.run(go())
    assert log == ["start", "end"]
    assert state.busy_with(1) is None


def test_a_deploy_restarts_what_it_built_without_waiting_for_itself(monkeypatch):
    """The deploy holds the app and restarts through restart_locked; the public restart would refuse it."""
    from apps import deployments

    calls = []

    async def fake_restart(app_id, db, actor):
        calls.append(("restart", state.busy_with(app_id)))
        return {}

    async def fake_replicas(app_id, db):
        return ["a-live-replica"]

    class Db:
        async def commit(self):
            pass

    class App:
        id = 7
        app_type = "node"
        start_command = "npm start"
        working_dir = "."
        cron_schedule = None
        port = 3000
        no_web = False

    monkeypatch.setattr(deployments, "restart_locked", fake_restart)
    monkeypatch.setattr(deployments.apps_runtime, "_load_app_replicas", fake_replicas)
    monkeypatch.setattr(deployments.apps_runtime, "_has_live_replicas", lambda replicas: True)
    monkeypatch.setattr(deployments.containers_build, "build_image_unless_current", lambda *a, **k: ("img", True))
    monkeypatch.setattr(deployments.apps_images, "_image_build_kwargs", lambda app: {})
    monkeypatch.setattr(deployments.apps_images, "_image_port", lambda app: 3000)

    async def go():
        with state.app_lock(7, "deploying"):
            return await deployments._rebuild(App(), Db(), "rev", lambda line: None, restart=True, actor="t")
    assert asyncio.run(go()) == {"restarted": True}
    assert calls == [("restart", "deploying")]


# ── through the real endpoints ────────────────────────────────────────────────
def _make_app(name):
    from core.database import AsyncSessionLocal
    from core.models import Application

    async def go():
        async with AsyncSessionLocal() as db:
            app = Application(name=name, repo_url="https://example.test/x", start_command="npm start", port=3000,
                              working_dir=tempfile.gettempdir())
            db.add(app)
            await db.commit()
            return app.id
    return asyncio.run(go())


BUSY_CALLS = [
    ("post", "/start", None),
    ("post", "/stop", None),
    ("post", "/restart", None),
    ("post", "/scale", {"count": 1}),
    ("post", "/instances/restart", {"ids": [1]}),
    ("post", "/instances/1/restart", None),
    ("post", "/instances/remove", {"ids": [1]}),
    ("delete", "/instances/1", None),
    ("post", "/deploy-rolling", None),
    ("post", "/deploy-blue-green", None),
    ("post", "/deploy/stream", None),
    ("delete", "", None),
]


@pytest.mark.parametrize("method,path,body", BUSY_CALLS, ids=[f"{m} {p or '/'}" for m, p, _ in BUSY_CALLS])
def test_every_action_on_a_busy_app_is_refused(admin, method, path, body):
    app_id = _make_app(f"busy{abs(hash((method, path))) % 10**6}")
    with state.app_lock(app_id, "deploying"):
        r = admin.request(method.upper(), f"/api/apps/{app_id}{path}", json=body)
    assert r.status_code == 409, r.text
    assert "deploying" in r.text
    assert state.busy_with(app_id) is None                   # nothing was left behind


def test_the_app_works_again_when_the_action_is_done(admin):
    app_id = _make_app("not-busy-anymore")
    with state.app_lock(app_id, "deploying"):
        assert admin.post(f"/api/apps/{app_id}/stop").status_code == 409
    r = admin.post(f"/api/apps/{app_id}/stop")
    assert r.status_code == 200, r.text
    assert state.busy_with(app_id) is None


def test_the_deployments_list_says_whether_a_deploy_is_running(admin):
    app_id = _make_app("deploy-flag")
    assert admin.get(f"/api/apps/{app_id}/deployments").json()["in_progress"] is False
    with state.app_lock(app_id, "deploying"):
        assert admin.get(f"/api/apps/{app_id}/deployments").json()["in_progress"] is True
    with state.app_lock(app_id, "scaling"):                   # busy with something else is not a deploy
        assert admin.get(f"/api/apps/{app_id}/deployments").json()["in_progress"] is False
