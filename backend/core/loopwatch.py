"""Says so, with the exact place, when something holds up the event loop.

Everything the panel does runs on one event loop. A call that waits (a subprocess, a file, a slow library)
without handing the loop back stops every other request until it is done: that is what a page that "hangs" is.
Such calls belong in asyncio.to_thread. This is the safety net for the ones that were missed: a thread checks
every half second that the loop still runs, and when it does not for a while it logs the stack of the thread
the loop runs in, which is the line that is in the way.
"""
import asyncio
import logging
import sys
import threading
import time
import traceback

log = logging.getLogger("cloudbase.loop")

CHECK_SECONDS = 0.5
BLOCKED_SECONDS = 2.0


def start(loop: asyncio.AbstractEventLoop, blocked_seconds: float = BLOCKED_SECONDS) -> threading.Thread:
    state = {"beat": time.monotonic(), "thread": None}

    def beat() -> None:
        state["beat"] = time.monotonic()
        state["thread"] = threading.get_ident()
        loop.call_later(CHECK_SECONDS, beat)

    loop.call_soon_threadsafe(beat)

    def watch() -> None:
        reported = False
        while not loop.is_closed():
            time.sleep(CHECK_SECONDS)
            lag = time.monotonic() - state["beat"] - CHECK_SECONDS
            if lag < blocked_seconds:
                reported = False
                continue
            if reported:
                continue
            reported = True
            frame = sys._current_frames().get(state["thread"])
            where = "".join(traceback.format_stack(frame)) if frame is not None else "(unknown)"
            log.warning("The event loop has been blocked for %.1fs; it is busy here:\n%s", lag, where)

    thread = threading.Thread(target=watch, name="loop-watch", daemon=True)
    thread.start()
    return thread
