"""The app's log says what really happens to its instances; the server writes it, from what it knows (the node's
report, promote's own checks, a node that goes away), not the browser guessing from an HTTP answer."""
import asyncio

import pytest


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def lines(monkeypatch):
    from containers import processes as pm
    said = []
    monkeypatch.setattr(pm, "_push_line", lambda app_id, line: said.append((app_id, line)))
    return said


@pytest.fixture
def make(client):
    from sqlalchemy import delete
    from core.database import AsyncSessionLocal
    from core.models import Application, Instance, Node
    made = {"apps": [], "nodes": []}

    def node(name):
        async def go():
            async with AsyncSessionLocal() as db:
                row = Node(name=name, status="online", auth_token=f"token-{name}", is_local=False, enabled=True, agent_version="4.0.0", wg_state="up")
                db.add(row)
                await db.commit()
                return row.id
        made["nodes"].append(_run(go()))
        return made["nodes"][-1]

    def app(name, node_id, status="starting", desired="running", spec_hash="h1", **fields):
        async def go():
            async with AsyncSessionLocal() as db:
                a = Application(name=name, repo_url="https://example.test/x", start_command="x", port=3000, **fields)
                db.add(a)
                await db.flush()
                inst = Instance(app_id=a.id, node_id=node_id, status=status, desired=desired, spec_hash=spec_hash,
                                external_port=13000 + a.id)
                db.add(inst)
                await db.commit()
                return a.id, inst.id
        app_id, instance_id = _run(go())
        made["apps"].append(app_id)
        return app_id, instance_id

    yield node, app

    async def cleanup():
        async with AsyncSessionLocal() as db:
            await db.execute(delete(Instance).where(Instance.app_id.in_(made["apps"])))
            await db.execute(delete(Application).where(Application.id.in_(made["apps"])))
            await db.execute(delete(Node).where(Node.id.in_(made["nodes"])))
            await db.commit()
    _run(cleanup())


def test_every_step_the_node_reports_is_written_once(make, lines, monkeypatch):
    from nodes import observed
    monkeypatch.setattr(observed.promote, "start_promotion", lambda instance_id: None)
    node, app = make
    node_id = node("ll-steps")
    app_id, iid = app("ll-steps-app", node_id)

    def report(**r):
        _run(observed.apply(node_id, {str(iid): {"spec_hash": "h1", **r}}))
    report(state="building", substatus="building_image")
    report(state="building", substatus="building_image")                     # nothing new: nothing written
    report(state="starting", substatus="creating_container")
    report(state="error", error="exit code 1")
    assert [line for a, line in lines if a == app_id] == [
        f"[Instance {iid}] building the image",
        f"[Instance {iid}] creating the container",
        f"[Instance {iid}] failed: exit code 1",
    ]


def test_a_stop_is_reported_when_the_node_has_really_stopped_it(make, lines):
    from nodes import observed
    node, app = make
    node_id = node("ll-stop")
    app_id, iid = app("ll-stop-app", node_id, status="stopping", desired="stopped")
    _run(observed.apply(node_id, {str(iid): {"state": "stopped"}}))
    assert lines == [(app_id, f"[Instance {iid}] stopped")]


def test_promote_says_when_it_checks_and_when_the_instance_is_live(make, lines):
    from nodes import promote
    node, app = make
    node_id = node("ll-live")
    app_id, iid = app("ll-live-app", node_id, no_web=True)                     # a worker: no port, live once it runs
    _run(promote.promote_when_reachable(iid))
    assert (app_id, f"[Instance {iid}] is live") in lines


def test_a_start_that_is_given_up_says_why(make, lines):
    from nodes import promote
    node, app = make
    node_id = node("ll-giveup")
    app_id, iid = app("ll-giveup-app", node_id)
    _run(promote._give_up(iid, "health check failed: 500"))
    assert (app_id, f"[Instance {iid}] failed: health check failed: 500") in lines


def test_a_start_given_up_while_the_node_network_is_down_says_why_too(make, lines):
    from core.database import AsyncSessionLocal
    from core.models import Node
    from nodes import promote
    node, app = make
    node_id = node("ll-linkdown")
    app_id, iid = app("ll-linkdown-app", node_id)

    async def link_down():
        async with AsyncSessionLocal() as db:
            (await db.get(Node, node_id)).wg_state = "down"
            await db.commit()
    _run(link_down())
    _run(promote._give_up(iid, "did not become reachable within 90s"))
    assert (app_id, f"[Instance {iid}] its node cannot be reached (did not become reachable within 90s)") in lines


def test_an_unreachable_node_is_said_for_each_of_its_instances(make, lines):
    from nodes import liveness
    node, app = make
    node_id = node("ll-offline")
    app_id, iid = app("ll-offline-app", node_id, status="running")
    _run(liveness.node_gone(node_id))
    assert (app_id, f"[Instance {iid}] its node cannot be reached") in lines


def test_the_actions_say_what_they_do_and_who_did_it(admin, make, lines, monkeypatch):
    from apps import nginx_sync
    monkeypatch.setattr(nginx_sync._syscfg, "get_base_domain_cached", lambda: "")
    node, app = make
    node_id = node("ll-actions")
    app_id, iid = app("ll-actions-app", node_id, status="running")
    assert admin.post(f"/api/apps/{app_id}/restart").status_code == 200
    assert admin.post(f"/api/apps/{app_id}/stop").status_code == 200
    said = [line for a, line in lines if a == app_id]
    assert "[Restart] Restarting 1 instance(s) by admin" in said
    assert "[Stop] Stopping 1 instance(s) by admin" in said


def test_the_browser_no_longer_writes_its_own_guesses_in_the_log():
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    assert "_logAction" not in js and "successful`" not in js
