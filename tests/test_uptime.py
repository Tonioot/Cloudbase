"""Uptime checks, the outages they add up to, and the public status page."""
import asyncio
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _app_started(admin):
    """The database exists once the app has started."""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        code = {"/ok": 200, "/missing": 404, "/boom": 500}.get(self.path, 200)
        self.send_response(code)
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


# ── asking an address ─────────────────────────────────────────────────────────

def test_a_healthy_answer_is_up_with_a_response_time(server):
    from apps import uptime
    ok, ms, detail = uptime.probe(server, "/ok", "")
    assert ok and ms is not None and detail == "HTTP 200"


def test_the_apps_health_check_settings_decide_what_is_healthy(server):
    from apps import uptime
    assert uptime.probe(server, "/missing", "")[0] is True            # empty: anything below 500
    assert uptime.probe(server, "/missing", "2xx")[0] is False
    assert uptime.probe(server, "/missing", "404")[0] is True
    ok, _, detail = uptime.probe(server, "/boom", "")
    assert not ok and detail == "HTTP 500 on /boom"


def test_nothing_listening_is_down_without_raising():
    from apps import uptime
    ok, ms, detail = uptime.probe("http://127.0.0.1:1", "/", "")
    assert not ok and ms is None and detail.startswith("no answer")


# ── outages ───────────────────────────────────────────────────────────────────

def _checks(pattern, start=datetime(2026, 11, 1)):
    return [(start + timedelta(minutes=i), c == "o") for i, c in enumerate(pattern)]


def test_one_failed_check_is_not_an_outage():
    from apps import uptime
    assert uptime.incidents(_checks("ooxoo")) == []


def test_two_failed_checks_in_a_row_are_an_outage_that_ends_with_the_next_good_one():
    from apps import uptime
    out = uptime.incidents(_checks("ooxxxoo"))
    assert len(out) == 1
    assert out[0]["from"] == datetime(2026, 11, 1, 0, 2) and out[0]["until"] == datetime(2026, 11, 1, 0, 5)


def test_an_outage_that_is_still_going_has_no_end_and_the_newest_comes_first():
    from apps import uptime
    out = uptime.incidents(_checks("xxooxx"))
    assert [i["until"] is None for i in out] == [True, False]


# ── what is kept ──────────────────────────────────────────────────────────────

def _app(name, **fields):
    from core.database import AsyncSessionLocal
    from core.models import Application

    async def go():
        async with AsyncSessionLocal() as db:
            app = Application(name=name, repo_url="https://example.test/x", start_command="x", port=3000, **fields)
            db.add(app)
            await db.commit()
            return app.id
    return _run(go())


def _record(app_id, results, start):
    from apps import uptime
    from core.database import AsyncSessionLocal

    async def go():
        async with AsyncSessionLocal() as db:
            for i, (ok, ms) in enumerate(results):
                await uptime.record(db, app_id, ok, ms, "HTTP 200" if ok else "no answer", start + timedelta(minutes=i))
    _run(go())


def _summary(app_id):
    from apps import uptime
    from core.database import AsyncSessionLocal

    async def go():
        async with AsyncSessionLocal() as db:
            return await uptime.summary(db, app_id)
    return _run(go())


def test_a_day_adds_up_to_a_percentage_and_an_average_response_time():
    app_id = _app("uptime-sums")
    now = datetime.utcnow().replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(hours=1)
    _record(app_id, [(True, 100), (True, 300), (False, None), (False, None)], now)
    data = _summary(app_id)
    assert data["uptime"] == 50.0 and data["average_ms"] == 200
    assert len(data["days"]) == 90 and data["days"][-1]["uptime"] == 50.0 and data["days"][0]["uptime"] is None
    assert data["last_check"]["ok"] is False
    assert len(data["incidents"]) == 1 and data["incidents"][0]["until"] is None


def test_old_checks_go_after_7_days_and_old_days_after_90():
    from apps import uptime
    from core.database import AsyncSessionLocal
    from core.models import UptimeCheck, UptimeDay
    from sqlalchemy import select
    app_id = _app("uptime-prune")
    now = datetime.utcnow()
    _record(app_id, [(True, 10)], now - timedelta(days=8))
    _record(app_id, [(True, 10)], now - timedelta(days=100))
    _record(app_id, [(True, 10)], now)

    async def go():
        async with AsyncSessionLocal() as db:
            await uptime.prune(db)
            checks = (await db.execute(select(UptimeCheck).where(UptimeCheck.app_id == app_id))).scalars().all()
            days = (await db.execute(select(UptimeDay).where(UptimeDay.app_id == app_id))).scalars().all()
            return len(checks), sorted(d.day for d in days)
    count, days = _run(go())
    assert count == 1
    assert days == [(now - timedelta(days=8)).date(), now.date()]


def test_what_an_app_is_doing_is_up_down_maintenance_stopped_or_unknown():
    from apps import uptime

    class A:
        maintenance_mode = False
    up, down = {"ok": True}, {"ok": False}
    assert uptime.state_of(A(), True, up) == "up"
    assert uptime.state_of(A(), True, down) == "down"
    assert uptime.state_of(A(), True, None) == "unknown"
    assert uptime.state_of(A(), False, down) == "stopped"        # stopped on purpose is not an outage
    A.maintenance_mode = True
    assert uptime.state_of(A(), True, down) == "maintenance"


# ── the outage notification ───────────────────────────────────────────────────

def test_two_failed_checks_raise_the_alert_and_a_good_one_clears_it(monkeypatch):
    from workers import uptime as worker
    events = []

    async def fake_emit(event, title, message="", **kw):
        events.append(("emit", event, kw.get("key")))

    async def fake_resolve(key, title, message="", **kw):
        events.append(("resolve", key))
    monkeypatch.setattr(worker, "emit", fake_emit)
    monkeypatch.setattr(worker, "resolve", fake_resolve)
    app_id = _app("uptime-alert")
    answers = iter([(False, None, "no answer"), (False, None, "no answer"), (False, None, "no answer"), (True, 20, "HTTP 200")])
    monkeypatch.setattr(worker.apps_uptime, "probe", lambda *a: next(answers))
    worker._failures.clear()
    for _ in range(4):
        _run(worker.check_app(app_id, "Site", "http://site.test", "/", ""))
    key = f"uptime.down:{app_id}"
    assert events == [("emit", "uptime.down", key), ("emit", "uptime.down", key), ("resolve", key)]


# ── the status page ───────────────────────────────────────────────────────────

def test_the_status_page_and_its_data_need_no_sign_in(client):
    from fastapi.testclient import TestClient
    import main
    anonymous = TestClient(main.app, base_url="http://localhost")   # not started again, and no cookies
    assert anonymous.get("/status").status_code == 200
    r = anonymous.get("/api/status")
    assert r.status_code == 200 and "apps" in r.json()


def test_only_apps_that_opted_in_are_on_the_status_page(admin):
    from routers import status
    shown = _app("status-shown", show_on_status_page=True)
    _app("status-hidden")
    status._cache = (0.0, {})
    names = [a["name"] for a in admin.get("/api/status").json()["apps"]]
    assert "status-shown" in names and "status-hidden" not in names
    status._cache = (0.0, {})
    assert shown


def test_showing_an_app_on_the_status_page_is_off_until_it_is_switched_on(admin):
    app_id = _app("status-setting")
    assert admin.get(f"/api/apps/{app_id}").json()["show_on_status_page"] is False
    assert admin.put(f"/api/apps/{app_id}", json={"show_on_status_page": True}).status_code == 200
    assert admin.get(f"/api/apps/{app_id}").json()["show_on_status_page"] is True


def test_the_panel_can_read_the_uptime_of_one_app(admin):
    app_id = _app("status-panel")
    r = admin.get(f"/api/apps/{app_id}/uptime")
    assert r.status_code == 200 and r.json()["status"] == "stopped" and r.json()["uptime"] is None


# ── how often ─────────────────────────────────────────────────────────────────

def test_only_the_offered_intervals_are_accepted(admin):
    from apps import uptime
    from fastapi import HTTPException
    assert [uptime.clean_interval(v) for v in (0, "30", 60, 900)] == [0, 30, 60, 900]
    for bad in (1, 45, 3600, "x", None):
        with pytest.raises(HTTPException):
            uptime.clean_interval(bad)
    app_id = _app("interval-setting")
    assert admin.get(f"/api/apps/{app_id}").json()["uptime_interval_seconds"] == 60
    assert admin.put(f"/api/apps/{app_id}", json={"uptime_interval_seconds": 300}).status_code == 200
    assert admin.get(f"/api/apps/{app_id}").json()["uptime_interval_seconds"] == 300
    assert admin.put(f"/api/apps/{app_id}", json={"uptime_interval_seconds": 7}).status_code == 400


def test_each_app_is_checked_at_its_own_pace_and_never_when_off(monkeypatch):
    from workers import uptime as worker
    asked = []

    async def targets():
        return [(1, "fast", "http://a.test", "/", "", 30), (2, "slow", "http://b.test", "/", "", 600)]

    async def check(app_id, *a):
        asked.append(app_id)

    async def open_none(prefix):
        return {}
    monkeypatch.setattr(worker, "_targets", targets)
    monkeypatch.setattr(worker, "check_app", check)
    monkeypatch.setattr(worker, "open_keys", open_none)
    worker._last_check.clear()
    clock = [1000.0]
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock[0])
    for _ in range(3):                      # now, +30 s, +60 s
        _run(worker.check_all())
        clock[0] += 30
    assert sorted(asked) == [1, 1, 1, 2]    # the slow one only the first time


def test_the_whole_page_is_only_operational_when_something_really_answers():
    from routers.status import overall_state
    assert overall_state([]) == "none"
    assert overall_state(["up", "up"]) == "up"
    assert overall_state(["up", "stopped"]) == "up"            # stopped on purpose does not spoil it
    assert overall_state(["up", "down", "maintenance"]) == "down"
    assert overall_state(["up", "maintenance"]) == "maintenance"
    assert overall_state(["stopped", "stopped"]) == "stopped"   # nothing runs: not "operational"
    assert overall_state(["stopped", "unknown"]) == "unknown"
    assert overall_state(["unknown"]) == "unknown"


def test_monitoring_asks_what_the_startup_check_asks_unless_it_has_its_own_path(admin):
    from workers import uptime as worker
    from core.database import AsyncSessionLocal
    from core.models import Instance
    same = _app("path-same", domain="same.test", health_path="/ready", health_expect="2xx")
    own = _app("path-own", domain="own.test", health_path="/ready", health_expect="2xx", uptime_path="/", uptime_expect="200,301")

    async def make_running():
        async with AsyncSessionLocal() as db:
            for app_id in (same, own):
                db.add(Instance(app_id=app_id, node_id=1, status="running"))
            await db.commit()
    _run(make_running())
    found = {t[1]: t for t in _run(worker._targets())}
    assert found["path-same"][3:5] == ("/ready", "2xx")
    assert found["path-own"][3:5] == ("/", "200,301")
    assert admin.put(f"/api/apps/{own}", json={"uptime_path": "no-slash"}).status_code == 400
