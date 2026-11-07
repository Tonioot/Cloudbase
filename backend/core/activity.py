"""What is happening to an app right now: restarting, deploying, scaling, building, ...

An app's status says what it *is* (running, stopped, error). It does not say that someone is in the
middle of restarting it, or that a deploy is waiting for a new instance on another node. That lives
here, in memory (an operation dies with the process anyway), and is added to the app in the panel.

    with activity.track(app_id, "restarting"):      # or @activity.tracked("restarting") on a function
        ...                                         # the step shown is the last "[Tag] message" the app logged

While an operation runs, every progress line Cloudbase writes for the app (the "[Rolling] ...", "[ZD] ..."
lines of its live log) becomes the current step, so the panel and the log always tell the same story.
"""
import contextlib
import functools
import time

# Lines of the build output are far too noisy to be a step
_NOT_A_STEP = ("[Docker build]",)

KINDS = {
    "starting": "Starting",
    "stopping": "Stopping",
    "restarting": "Restarting",
    "deploying": "Deploying",
    "building": "Building the image",
    "scaling": "Scaling",
    "updating": "Updating",
}

_current: dict[int, list[dict]] = {}


def begin(app_id: int, kind: str, detail: str = "") -> dict:
    entry = {"kind": kind, "detail": detail, "since": time.time()}
    _current.setdefault(app_id, []).append(entry)
    return entry


def end(app_id: int, entry: dict) -> None:
    stack = _current.get(app_id) or []
    if entry in stack:
        stack.remove(entry)
    if not stack:
        _current.pop(app_id, None)


@contextlib.contextmanager
def track(app_id: int, kind: str, detail: str = ""):
    """Show `kind` for this app for as long as the block runs (also when it fails)."""
    entry = begin(app_id, kind, detail)
    try:
        yield entry
    finally:
        end(app_id, entry)


def tracked(kind: str, detail: str = ""):
    """Decorator for an async function whose first argument (or app_id=) is the app id."""
    def wrap(fn):
        @functools.wraps(fn)
        async def inner(*args, **kwargs):
            app_id = kwargs.get("app_id", args[0] if args else None)
            with track(app_id, kind, detail):
                return await fn(*args, **kwargs)
        return inner
    return wrap


def step(app_id: int, detail: str) -> None:
    """Say what the operation is doing now (no effect when nothing is going on)."""
    stack = _current.get(app_id)
    if stack:
        stack[-1]["detail"] = detail


def note(app_id: int, line: str) -> None:
    """Called for every progress line Cloudbase writes for an app: tagged lines become the step."""
    if app_id in _current and line.startswith("[") and not line.startswith(_NOT_A_STEP):
        step(app_id, line.strip()[:300])


def get(app_id: int) -> dict | None:
    """The current operation, or None. The kind is that of the outermost operation (a deploy that restarts
    something is still 'deploying'); the step is the latest of any."""
    stack = _current.get(app_id)
    if not stack:
        return None
    first = stack[0]
    return {"kind": first["kind"], "label": KINDS.get(first["kind"], first["kind"].capitalize()),
            "detail": stack[-1]["detail"], "since": int(first["since"]), "elapsed": int(time.time() - first["since"])}


def busy(app_id: int) -> bool:
    return app_id in _current
