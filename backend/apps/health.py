"""The health check of an app: when is a fresh instance ready for visitors?

An instance only goes into nginx once the primary can connect to it AND it answers a request the way this app
says is healthy. Three settings per app:

* `health_path`     the URL path to ask, "/" by default; empty means "only check that the port is open"
* `health_expect`   what counts as healthy: empty = any answer below 500; otherwise a comma separated list of
                    status codes (`200`) and classes (`2xx`, `3xx`), for example `2xx,301`
* `health_timeout_seconds`  how long a new instance gets to become healthy before it is marked as failed
"""
import asyncio
import http.client
import re
from typing import Optional

from fastapi import HTTPException

DEFAULT_PATH = "/"
DEFAULT_TIMEOUT_SECONDS = 600
MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS = 10, 3600
_CLASS_RE = re.compile(r"^[1-5]xx$")
_PATH_RE = re.compile(r"^/[^\s]*$")


def parse_expect(text: Optional[str]) -> list[str]:
    return [t.strip().lower() for t in (text or "").split(",") if t.strip()]


def is_healthy(status: int, expect: Optional[str]) -> bool:
    tokens = parse_expect(expect)
    if not tokens:
        return 100 <= status < 500
    for token in tokens:
        if _CLASS_RE.match(token) and status // 100 == int(token[0]):
            return True
        if token.isdigit() and status == int(token):
            return True
    return False


def clean_path(path: Optional[str]) -> str:
    """The path as it is stored: "" (port check only) or a path starting with a slash. Raises HTTPException(400)."""
    path = (path or "").strip()
    if not path:
        return ""
    if len(path) > 200 or not _PATH_RE.match(path):
        raise HTTPException(400, "The health check path must start with / and contain no spaces")
    return path


def clean_expect(text: Optional[str]) -> str:
    tokens = parse_expect(text)
    for token in tokens:
        if not (_CLASS_RE.match(token) or (token.isdigit() and 100 <= int(token) <= 599)):
            raise HTTPException(400, f"'{token}' is not a status code (200) or a class (2xx) for the health check")
    return ",".join(tokens)


def clean_timeout(seconds) -> int:
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        raise HTTPException(400, "The health check time-out must be a number of seconds")
    return max(MIN_TIMEOUT_SECONDS, min(value, MAX_TIMEOUT_SECONDS))


def settings_of(app) -> tuple[str, str, int]:
    """(path, expect, timeout) of an app; apps from before the setting existed get the defaults."""
    path = getattr(app, "health_path", None)
    return (DEFAULT_PATH if path is None else path,
            getattr(app, "health_expect", None) or "",
            getattr(app, "health_timeout_seconds", None) or DEFAULT_TIMEOUT_SECONDS)


def answer(host: str, port: int, path: str = DEFAULT_PATH, expect: str = "") -> tuple[bool, str]:
    """Ask the instance once. Returns (healthy, what happened). A blocking call: run it with asyncio.to_thread."""
    try:
        conn = http.client.HTTPConnection(host, int(port), timeout=2.0)
        try:
            if not path:
                conn.connect()
                return True, "port is open"
            conn.request("GET", path, headers={"User-Agent": "Cloudbase-health-check", "Connection": "close"})
            status = int(conn.getresponse().status or 0)
        finally:
            conn.close()
    except Exception as exc:
        return False, f"no answer ({type(exc).__name__})"
    return (True, f"HTTP {status}") if is_healthy(status, expect) else (False, f"HTTP {status} on {path}")


async def wait_until_healthy(backend: str, app, stop_when=None, poll_seconds: float = 2.0) -> tuple[bool, str]:
    """Ask the instance until it is healthy or the app's time-out is over.

    `stop_when()` is an optional async function that returns a reason to give up early (the instance was removed,
    its node reported an error)."""
    host, _, port = backend.rpartition(":")
    path, expect, timeout = settings_of(app)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last = "no answer yet"
    while True:
        ok, last = await asyncio.to_thread(answer, host, int(port), path, expect)
        if ok:
            return True, last
        if stop_when is not None:
            reason = await stop_when()
            if reason:
                return False, reason
        if loop.time() >= deadline:
            return False, f"not healthy within {timeout}s (last: {last})"
        await asyncio.sleep(poll_seconds)
