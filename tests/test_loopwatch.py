"""The watchdog that says where the event loop is stuck (core/loopwatch.py)."""
import asyncio
import logging
import time

from core import loopwatch


def test_a_blocked_event_loop_is_reported_with_the_line_that_blocks_it(caplog):
    def blocker():
        time.sleep(1.6)            # a call that never hands the loop back

    async def go():
        loopwatch.start(asyncio.get_running_loop(), blocked_seconds=0.8)
        await asyncio.sleep(0.7)
        blocker()
        await asyncio.sleep(0.7)

    with caplog.at_level(logging.WARNING, logger="cloudbase.loop"):
        asyncio.run(go())
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "blocked for" in text and "blocker" in text


def test_a_loop_that_keeps_running_is_left_alone(caplog):
    async def go():
        loopwatch.start(asyncio.get_running_loop(), blocked_seconds=0.8)
        for _ in range(8):
            await asyncio.sleep(0.2)

    with caplog.at_level(logging.WARNING, logger="cloudbase.loop"):
        asyncio.run(go())
    assert not [r for r in caplog.records if r.name == "cloudbase.loop"]
