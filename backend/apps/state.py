import contextlib
import functools
from typing import Optional

from fastapi import HTTPException

# New instances during rolling / blue-green deploys. Generous because some start
# commands build at boot (e.g. `next build && next start`), which takes minutes
# on small nodes like a Raspberry Pi. A crashed container fails fast regardless.
DEPLOY_HEALTH_TIMEOUT_SECONDS = 600


DEPLOY_HEALTH_PROGRESS_SECONDS = 30


# One action at a time per app. Whoever changes an app's instances (deploy, start, stop, restart, scale,
# removing an instance, delete, the autoscaler) holds the app for the duration; a second one is refused
# with a 409 instead of racing the first. The panel is one process with one event loop and the check and
# the claim below have no await between them, so a plain dict is enough.
_busy: dict[int, str] = {}


def busy_with(app_id: int) -> Optional[str]:
    """What is being done to the app right now ("deploying", "scaling", ...), or None."""
    return _busy.get(app_id)


def acquire_app(app_id: int, what: str) -> None:
    """Claim the app for `what`, or raise 409 when something else has it. Pair with release_app()."""
    if app_id in _busy:
        raise HTTPException(409, f"This app is busy ({_busy[app_id]}) — try again when that is done.")
    _busy[app_id] = what


def release_app(app_id: int) -> None:
    _busy.pop(app_id, None)


@contextlib.contextmanager
def app_lock(app_id: int, what: str):
    acquire_app(app_id, what)
    try:
        yield
    finally:
        release_app(app_id)


def locked(what: str):
    """Decorator for an async function whose first argument (or app_id=) is the app id."""
    def wrap(fn):
        @functools.wraps(fn)
        async def inner(*args, **kwargs):
            with app_lock(kwargs.get("app_id", args[0] if args else None), what):
                return await fn(*args, **kwargs)
        return inner
    return wrap
