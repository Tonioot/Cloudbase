"""What is happening to an app right now (core/activity.py), and that the panel can see it."""
import asyncio
import inspect

import pytest

from core import activity


@pytest.fixture(autouse=True)
def _clean():
    activity._current.clear()
    yield
    activity._current.clear()


def test_nothing_is_going_on_by_default():
    assert activity.get(1) is None and not activity.busy(1)


def test_an_operation_shows_while_it_runs_and_is_gone_afterwards():
    with activity.track(1, "restarting"):
        info = activity.get(1)
        assert info["kind"] == "restarting" and info["label"] == "Restarting" and info["elapsed"] >= 0 and info["since"] > 0
        assert activity.busy(1) and activity.get(2) is None
    assert activity.get(1) is None


def test_it_is_gone_also_when_the_operation_fails():
    with pytest.raises(RuntimeError):
        with activity.track(1, "deploying"):
            raise RuntimeError("boom")
    assert activity.get(1) is None


def test_the_latest_progress_line_is_the_step():
    with activity.track(1, "deploying"):
        activity.note(1, "[Rolling] Waiting for instance 12 on 'ToGPC' to be built and reachable (max 15 min)…")
        assert activity.get(1)["detail"].startswith("[Rolling] Waiting for instance 12")
        activity.note(1, "[Rolling] Health checking remote instance 12 on 10.77.0.2:9101…")
        assert "Health checking" in activity.get(1)["detail"]


def test_build_output_and_plain_lines_are_not_steps():
    with activity.track(1, "building", "Building the image"):
        activity.note(1, "[Docker build] #8 31.24 Downloading pypng-0.20220715.0-py3-none-any.whl (58 kB)")
        activity.note(1, "some output of the app itself")
        assert activity.get(1)["detail"] == "Building the image"


def test_lines_for_an_app_nothing_is_happening_to_change_nothing():
    activity.note(1, "[Rolling] hello")
    activity.step(1, "x")
    assert activity.get(1) is None


def test_a_deploy_that_restarts_something_is_still_a_deploy():
    with activity.track(1, "deploying"):
        with activity.track(1, "restarting", "Restarting instances"):
            info = activity.get(1)
            assert info["kind"] == "deploying" and info["detail"] == "Restarting instances"
        assert activity.get(1)["kind"] == "deploying"
    assert activity.get(1) is None


def test_two_operations_on_different_apps_do_not_mix():
    with activity.track(1, "starting"), activity.track(2, "stopping"):
        assert activity.get(1)["kind"] == "starting" and activity.get(2)["kind"] == "stopping"
        activity.step(1, "one")
        assert activity.get(2)["detail"] == ""


def test_the_decorator_tracks_the_whole_call_and_keeps_the_signature():
    seen = []

    @activity.tracked("scaling")
    async def op(app_id: int, db=None, *, actor="x"):
        seen.append(activity.get(app_id)["kind"])
        return "done"

    assert asyncio.run(op(7, actor="me")) == "done" and seen == ["scaling"] and activity.get(7) is None
    assert asyncio.run(op(app_id=8)) == "done" and seen == ["scaling", "scaling"]
    assert list(inspect.signature(op).parameters) == ["app_id", "db", "actor"]            # FastAPI still sees the real parameters


def test_the_live_log_of_an_app_feeds_the_step():
    from containers import processes as pm
    with activity.track(3, "deploying"):
        pm._push_line(3, "[ZD] Health checking instance 9 on 10.77.0.2:9101…")
        assert "Health checking instance 9" in activity.get(3)["detail"]


def _app(name):
    from core.database import AsyncSessionLocal
    from core.models import Application

    async def go():
        async with AsyncSessionLocal() as db:
            app = Application(name=name, repo_url="https://example.test/x", start_command="x", port=3000, external_port=9000, status="running")
            db.add(app)
            await db.commit()
            return app.id
    return asyncio.run(go())


def test_the_panel_gets_it_with_the_app(admin):
    app_id = _app("activity-app")
    assert admin.get(f"/api/apps/{app_id}").json()["activity"] is None
    with activity.track(app_id, "deploying", "[ZD] Switching nginx to the new instances"):
        shown = admin.get(f"/api/apps/{app_id}").json()["activity"]
        assert shown["kind"] == "deploying" and shown["label"] == "Deploying" and "Switching nginx" in shown["detail"]
        assert any(a["id"] == app_id and a["activity"] for a in admin.get("/api/apps").json())
    assert admin.get(f"/api/apps/{app_id}").json()["activity"] is None


def test_every_operation_that_changes_an_app_is_tracked():
    """If someone adds a new way to restart or deploy, it has to show up in the panel too."""
    from pathlib import Path
    backend = Path(__file__).resolve().parent.parent / "backend"
    expected = {
        "apps/lifecycle.py": ['@activity.tracked("restarting")', '@activity.tracked("starting")'],
        "apps/deployments.py": ['@activity.tracked("deploying")'],
        "apps/rolling.py": ['@activity.tracked("deploying")'],
        "apps/bluegreen.py": ['@activity.tracked("deploying")'],
        "routers/applications/lifecycle.py": ['@activity.tracked("stopping")'],
        "routers/applications/scale.py": ['@activity.tracked("scaling")'],
        "routers/applications/instances.py": ['@activity.tracked("restarting")'],
        "routers/applications/deploys.py": ['@activity.tracked("updating")', '@activity.tracked("building")', 'activity.begin(app_id, "building"'],
        "workers/autoscaler.py": ['activity.track(app_id, "scaling")'],
    }
    for path, needles in expected.items():
        text = (backend / path).read_text(encoding="utf-8")
        for needle in needles:
            assert needle in text, f"{path}: {needle}"


def test_a_deploy_takes_the_old_instance_out_of_running_before_it_stops_the_container():
    """Otherwise the crash monitor takes the stop for a crash ("Instance 84 exited") and may restart it."""
    from pathlib import Path
    backend = Path(__file__).resolve().parent.parent / "backend"
    for path, stop in (("apps/rolling.py", "stop_replica_container, app_id, old_replica.id"),
                       ("apps/bluegreen.py", "stop_replica_container, app_id, old_r.id")):
        text = (backend / path).read_text(encoding="utf-8")
        assert text.index("_mark_stopping(") < text.index(stop), path


# ── the panel shows it in the status itself ────────────────────────────────────────
FRONTEND = __import__("pathlib").Path(__file__).resolve().parent.parent / "frontend"


def test_every_kind_of_work_has_a_status_label_and_colour():
    """The badge in the status line says what is being done: each kind needs a label and the busy colour."""
    import re
    utils = (FRONTEND / "js" / "utils.js").read_text(encoding="utf-8")
    css = (FRONTEND / "css" / "components.css").read_text(encoding="utf-8")
    for kind in activity.KINDS:
        assert re.search(rf"\b{kind}\s*:\s*'", utils), f"no label for {kind}"
        assert f".badge-{kind}" in css, f"no colour for {kind}"


def test_there_is_no_separate_bar_for_it():
    page = (FRONTEND / "app.html").read_text(encoding="utf-8")
    js = (FRONTEND / "js" / "app.js").read_text(encoding="utf-8")
    assert "app-activity" not in page and "app-activity" not in js
    assert "badge(busyWith || app.status)" in js                 # the badge itself shows what is being done


def test_the_search_field_of_the_command_menu_has_no_square_outline():
    css = (FRONTEND / "css" / "base.css").read_text(encoding="utf-8")
    assert ".palette-input input:focus-visible { outline: none" in css



def test_the_command_menu_keeps_its_size_while_you_type():
    css = (FRONTEND / "css" / "base.css").read_text(encoding="utf-8")
    palette = css[css.index(".palette {"):css.index("@keyframes palette-in")]
    assert "height: min(" in palette and "max-height" not in palette
