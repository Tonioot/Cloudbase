"""The health check of an app: a new instance only goes live once it answers as the app says (apps/health.py)."""
import asyncio
import http.server
import threading

import pytest
from fastapi import HTTPException

from apps import health


def _run(coro):
    return asyncio.run(coro)


# ── what counts as healthy ─────────────────────────────────────────────────────
@pytest.mark.parametrize("status,expect,healthy", [
    (200, "", True), (404, "", True), (499, "", True), (500, "", False), (503, "", False),
    (200, "2xx", True), (301, "2xx", False), (301, "2xx,301", True), (204, "200", False), (200, "200", True),
    (500, "5xx", True),
])
def test_what_counts_as_healthy(status, expect, healthy):
    assert health.is_healthy(status, expect) is healthy


def test_settings_are_checked_when_they_are_saved():
    assert health.clean_path("") == "" and health.clean_path(" /up ") == "/up"
    assert health.clean_expect("2XX, 301") == "2xx,301" and health.clean_expect("") == ""
    assert health.clean_timeout(5) == health.MIN_TIMEOUT_SECONDS and health.clean_timeout(99999) == health.MAX_TIMEOUT_SECONDS
    for bad in ("up", "/a b", "/" + "x" * 300):
        with pytest.raises(HTTPException):
            health.clean_path(bad)
    for bad in ("fine", "6xx", "99", "2xx,ok"):
        with pytest.raises(HTTPException):
            health.clean_expect(bad)
    with pytest.raises(HTTPException):
        health.clean_timeout("soon")


def test_empty_settings_fall_back_to_the_defaults():
    class Unset:
        health_path, health_expect, health_timeout_seconds = None, None, None
    assert health.settings_of(Unset()) == ("/", "", health.DEFAULT_TIMEOUT_SECONDS)


# ── asking a real instance ──────────────────────────────────────────────────────
@pytest.fixture
def server():
    """A web server whose answers the test decides."""
    answers = {"/": 200, "/up": 200}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(answers.get(self.path, 404))
            self.end_headers()

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1], answers
    httpd.shutdown()


def test_the_instance_is_asked_on_the_path_of_the_app(server):
    port, answers = server
    answers["/up"] = 503
    assert health.answer("127.0.0.1", port, "/", "")[0] is True
    assert health.answer("127.0.0.1", port, "/up", "")[0] is False          # started, but not ready
    answers["/up"] = 200
    assert health.answer("127.0.0.1", port, "/up", "200")[0] is True
    assert health.answer("127.0.0.1", port, "/missing", "2xx")[0] is False


def test_an_empty_path_only_checks_that_the_port_is_open(server):
    port, answers = server
    answers["/"] = 500
    assert health.answer("127.0.0.1", port, "", "")[0] is True
    assert health.answer("127.0.0.1", 1, "", "")[0] is False                  # nothing listens there


def test_waiting_ends_when_the_instance_turns_healthy_and_gives_up_otherwise(server):
    port, answers = server
    app = type("App", (), {"health_path": "/up", "health_expect": "", "health_timeout_seconds": 10})()
    answers["/up"] = 503

    async def flip():
        await asyncio.sleep(0.6)
        answers["/up"] = 200

    async def go():
        asyncio.get_running_loop().create_task(flip())
        return await health.wait_until_healthy(f"127.0.0.1:{port}", app, poll_seconds=0.2)
    assert _run(go())[0] is True

    async def stopped():
        return "taken over"
    answers["/up"] = 503
    ok, detail = _run(health.wait_until_healthy(f"127.0.0.1:{port}", app, stopped, poll_seconds=0.1))
    assert not ok and detail == "taken over"
