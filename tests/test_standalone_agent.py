"""A node runs only its agent: no web server, no database, none of the panel's code."""
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent import alive

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"


@pytest.fixture
def alive_file(tmp_path, monkeypatch):
    path = tmp_path / "agent.alive"
    monkeypatch.setattr(alive.paths, "AGENT_ALIVE_FILE", str(path))
    return path


def test_the_agent_keeps_a_file_fresh_so_scripts_can_see_it_lives(alive_file):
    alive.set_connected(False)
    alive.write(now=1000)
    assert json.loads(alive_file.read_text()) == {"pid": os.getpid(), "at": 1000, "connected": False}
    alive.set_connected(True)
    alive.write(now=1005)
    assert json.loads(alive_file.read_text())["connected"] is True
    alive.set_connected(False)


def test_the_file_is_removed_when_the_agent_stops(alive_file):
    async def go():
        task = asyncio.ensure_future(alive.keep_fresh())
        await asyncio.sleep(0.05)
        assert alive_file.exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(go())
    assert not alive_file.exists()


def test_the_file_is_never_half_written(alive_file):
    for i in range(50):
        alive.write(now=i)
        assert json.loads(alive_file.read_text())["at"] == i
    assert [p.name for p in alive_file.parent.iterdir()] == ["agent.alive"]        # no temp files left behind


def test_the_agent_does_not_load_the_panel(tmp_path):
    """Importing everything the agent runs must not pull in the web app, its routers, workers or nginx code."""
    code = ("import sys; sys.path.insert(0, %r); import node_agent, agent.runner, agent.dispatch, agent.reconcile; "
            "bad = sorted(m for m in sys.modules if m.split('.')[0] in ('fastapi','starlette','uvicorn','routers','workers','main','nginx','notify','cron','transfer','nodes')); "
            "print(bad)") % str(BACKEND)
    home = str(tmp_path)
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60, cwd=str(tmp_path),
                         env={**os.environ, "HOME": home, "USERPROFILE": home, "PYTHONPATH": ""})
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "[]", out.stdout


def test_run_mode_and_registration_mode(monkeypatch):
    import node_agent
    calls = []

    async def fake_run(*registration):
        calls.append(("run", registration))

    async def fake_start(*args, **kwargs):
        calls.append(("start", args, kwargs))

    monkeypatch.setattr(node_agent, "run", fake_run)
    monkeypatch.setattr(node_agent, "start_agent", fake_start)

    monkeypatch.setattr(sys, "argv", ["node_agent.py"])
    asyncio.run(node_agent.main())
    monkeypatch.setattr(sys, "argv", ["node_agent.py", "--main-url", "https://p", "--invite-code", "x", "--exit-after-registration"])
    asyncio.run(node_agent.main())
    monkeypatch.setattr(sys, "argv", ["node_agent.py", "--main-url", "https://p", "--invite-code", "x", "--node-name", "pi"])
    asyncio.run(node_agent.main())
    assert calls[0] == ("run", (None, None, None, None, 15))                            # the service: state comes from the file
    assert calls[1][0] == "start" and calls[1][2] == {"exit_after_registration": True}      # the installer: register, then exit
    assert calls[2] == ("run", ("https://p", "x", "pi", None, 15))                      # register and keep running


def test_the_runner_starts_the_agent_and_cleans_up(alive_file, monkeypatch):
    from agent import runner
    seen = []

    async def fake_start(*registration):
        seen.append(("started", registration, alive_file.exists()))

    monkeypatch.setattr(runner, "start_agent", fake_start)
    monkeypatch.setattr(runner.system_logs, "setup_logging", lambda: seen.append("logging"))
    monkeypatch.setattr(runner.pm, "set_main_loop", lambda loop: seen.append("loop"))
    asyncio.run(runner.run("https://p"))
    assert seen[:2] == ["logging", "loop"] and seen[2][0] == "started" and seen[2][1] == ("https://p",)
    assert not alive_file.exists()


def test_the_scripts_start_the_agent_on_its_own_and_look_for_the_file():
    start = (ROOT / "start.sh").read_text(encoding="utf-8")
    install = (ROOT / "install.sh").read_text(encoding="utf-8")
    node_runtime = start[start.index("run_node_runtime() {"):start.index("case \"$COMMAND\" in start|run|help")]
    assert 'node_agent.py' in node_runtime and "uvicorn" not in node_runtime
    assert "agent.alive" in start and "agent.alive" in install


def test_a_web_app_started_on_a_node_serves_nothing_and_starts_no_loops():
    lifespan = (BACKEND / "lifespan.py").read_text(encoding="utf-8")
    assert "if IS_NODE:" in lifespan and "agent_loop" not in lifespan


def test_the_process_that_stays_alive_is_young_enough(alive_file):
    alive.write(now=time.time() - alive.STALE_SECONDS - 5)
    assert time.time() - json.loads(alive_file.read_text())["at"] > alive.STALE_SECONDS        # start.sh treats this as not running
