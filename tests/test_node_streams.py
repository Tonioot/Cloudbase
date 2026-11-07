"""Live streams a node sends to the primary: the agent calls the functions, not its own server."""
import asyncio
import json

import pytest

from agent import dispatch
from apps import node_streams


REAL_SLEEP = asyncio.sleep


async def _quick_sleep(seconds):
    await REAL_SLEEP(0)


def _run(coro):
    return asyncio.run(coro)


async def _until_cancelled(task, frames, wanted):
    for _ in range(200):
        if len(frames) >= wanted:
            break
        await REAL_SLEEP(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_system_stats_replay_the_history_and_then_keep_coming(monkeypatch):
    monkeypatch.setattr(node_streams, "_system_history", node_streams.deque([{"timestamp": 1, "cpu_percent": 5.0}], maxlen=60))
    monkeypatch.setattr(node_streams.psutil, "cpu_percent", lambda interval=None: 12.5)
    monkeypatch.setattr(node_streams.asyncio, "sleep", _quick_sleep)
    frames = []

    async def send(frame):
        frames.append(frame)

    async def go():
        task = asyncio.ensure_future(node_streams.stream_system_stats(send))
        await _until_cancelled(task, frames, 3)
    _run(go())
    assert frames[0] == {"timestamp": 1, "cpu_percent": 5.0}               # the replay
    assert frames[1]["cpu_percent"] == 12.5 and {"memory_percent", "disk_percent", "timestamp"} <= set(frames[1])
    assert len(node_streams._system_history) >= 2                          # and is remembered for the next viewer


def test_container_stats_are_sent_with_a_timestamp(monkeypatch):
    async def aggregate(app_id):
        return {"status": "running", "app": app_id, "cpu_percent": 3.0}
    monkeypatch.setattr(node_streams.containers_stats_logs, "app_aggregate_stats", aggregate)
    monkeypatch.setattr(node_streams.asyncio, "sleep", _quick_sleep)
    frames = []

    async def send(frame):
        frames.append(frame)

    async def go():
        await _until_cancelled(asyncio.ensure_future(node_streams.stream_container_stats(7, send)), frames, 2)
    _run(go())
    assert frames[0]["app"] == 7 and frames[0]["timestamp"] > 0


def test_the_relay_wraps_every_frame_for_the_primary(monkeypatch):
    sent = []

    async def send(text):
        sent.append(json.loads(text))

    async def fake_logs(app_id, send_text):
        await send_text("line one\n")
        await send_text("line two\n")
        raise asyncio.CancelledError

    async def fake_stats(app_id, send_frame):
        await send_frame({"cpu_percent": 1.5})
        raise asyncio.CancelledError

    monkeypatch.setattr(dispatch.node_streams, "follow_container_logs", fake_logs)
    monkeypatch.setattr(dispatch.node_streams, "stream_container_stats", fake_stats)

    async def go():
        for kind in ("stream_logs", "stream_stats"):
            with pytest.raises(asyncio.CancelledError):
                await dispatch._stream_relay(send, f"id-{kind}", kind, 7)
    _run(go())
    assert [m["data"] for m in sent] == ["line one\n", "line two\n", json.dumps({"cpu_percent": 1.5})]
    assert {m["type"] for m in sent} == {"stream_data"} and sent[0]["stream_id"] == "id-stream_logs"


def test_a_stream_command_starts_a_relay_and_can_be_cancelled(monkeypatch):
    seen = []

    async def fake_relay(send_fn, stream_id, kind, app_id):
        seen.append((kind, app_id))
        await asyncio.sleep(10)

    monkeypatch.setattr(dispatch, "_stream_relay", fake_relay)

    class WS:
        async def send(self, text):
            pass

    async def go():
        result = await dispatch._execute_command(None, None, {"command_type": "stream_stats", "app_id": 4, "payload": {"stream_id": "s1"}}, ws_main=WS())
        await asyncio.sleep(0)
        task = dispatch._active_streams["s1"]
        task.cancel()
        return result
    status, res, err = _run(go())
    assert (status, res, err) == ("streaming", {"stream_id": "s1"}, None) and seen == [("stream_stats", 4)]
    assert _run(dispatch._execute_command(None, None, {"command_type": "stream_logs", "app_id": 4, "payload": {}}))[0] == "failed"


def test_no_local_websocket_or_agent_token_is_left():
    from pathlib import Path
    backend = Path(__file__).resolve().parent.parent / "backend"
    for path in backend.rglob("*.py"):
        if "venv" in path.parts or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for gone in ("X-Agent-Token", "x-agent-token", "get_or_create_agent_token", "verify_agent_token", "_local_ws_url"):
            assert gone not in text, f"{path.name}: {gone}"
