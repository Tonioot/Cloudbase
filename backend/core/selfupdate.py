"""Update this installation from its Git remote, from inside Cloudbase and without root.

Used by the panel for the primary itself and by the node agent for a node (the "Update Cloudbase" button on a
node's page). It does the part of `cloudbase update` that needs no root: fetch the branch this checkout follows
and reset to it, then restart the service. systemd starts the new code (`Restart=on-failure`), and `start.sh run`
installs the Python dependencies before it starts.

What needs root stays with the installer: system packages, nginx, the service file and the root-owned helpers.
When the new version changes any of those (see SYSTEM_PATHS) nothing is touched and the answer says to run
`cloudbase update` on that server instead, so a server is never left half updated."""
import atexit
import functools
import os
import signal
import subprocess
import threading
from typing import Optional

from core import paths

# Files whose changes have to be installed as root by `cloudbase update`
SYSTEM_PATHS = ("install.sh", "scripts/cloudbase-", "scripts/setup-")

RESTART_EXIT_CODE = 75      # any non-zero exit makes systemd start the service again
FETCH_TIMEOUT = 180


def _git(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    return subprocess.run(["git", "-C", paths.REPO_DIR, *args], capture_output=True, text=True, timeout=timeout, env=env)


def _out(*args: str) -> str:
    r = _git(*args)
    return r.stdout.strip() if r.returncode == 0 else ""


@functools.lru_cache(maxsize=1)
def current() -> dict:
    """What this installation runs: {"commit": "abc1234", "version": "2.0.0"}. Fixed until the next start."""
    version = ""
    try:
        with open(paths.VERSION_FILE, encoding="utf-8") as f:
            version = f.read().strip()
    except OSError:
        pass
    return {"commit": _out("rev-parse", "--short", "HEAD"), "version": version}


def under_systemd() -> bool:
    return bool(os.environ.get("INVOCATION_ID"))


def needs_system(changed: list[str]) -> list[str]:
    return [p for p in changed if p.startswith(SYSTEM_PATHS)]


def update() -> dict:
    """Fetch and apply the newest version of the branch this checkout follows. Blocking.

    Returns {"status": ..., "message": ...} with status "current" (nothing new), "updated" (the caller must now call
    restart_soon), "needs_system" (run `cloudbase update` on the server) or "error"."""
    if not os.path.isdir(os.path.join(paths.REPO_DIR, ".git")):
        return {"status": "error", "message": "This installation is not a Git checkout, so it can't update itself. Run: cloudbase update"}
    if not under_systemd():
        return {"status": "error", "message": "Cloudbase is not running as a service here, so it can't restart itself. Run: cloudbase update"}
    branch = _out("rev-parse", "--abbrev-ref", "HEAD")
    if not branch or branch == "HEAD":
        branch = "main"
    if _out("status", "--porcelain", "--untracked-files=no"):
        return {"status": "error", "message": "Files in the installation were changed by hand and an update would overwrite them. Run: cloudbase update"}
    try:
        fetched = _git("fetch", "origin", branch, timeout=FETCH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": "Fetching the new version took too long"}
    if fetched.returncode != 0:
        return {"status": "error", "message": f"Could not fetch the new version: {(fetched.stderr or fetched.stdout).strip()[-300:]}"}

    old = _out("rev-parse", "--short", "HEAD")
    behind = _out("rev-list", "--count", f"HEAD..origin/{branch}")
    if behind in ("", "0"):
        return {"status": "current", "message": "Already up to date", "commit": old, "version": current()["version"]}
    changed = _out("diff", "--name-only", f"HEAD..origin/{branch}").splitlines()
    system = needs_system(changed)
    if system:
        return {"status": "needs_system", "files": system, "commit": old,
                "message": "This version also changes system files (the installer or the node network). Run on this server: cloudbase update"}
    reset = _git("reset", "--hard", f"origin/{branch}")
    if reset.returncode != 0:
        return {"status": "error", "message": f"Could not apply the new version: {(reset.stderr or reset.stdout).strip()[-300:]}"}
    new = _out("rev-parse", "--short", "HEAD")
    version = ""
    try:
        with open(paths.VERSION_FILE, encoding="utf-8") as f:
            version = f.read().strip()
    except OSError:
        pass
    return {"status": "updated", "message": "Updated; Cloudbase is restarting", "from": old, "commit": new, "version": version}


def restart_soon(graceful: bool, delay: float = 3.0) -> None:
    """Exit with a non-zero code after `delay` seconds, so systemd starts the service again.

    The panel (graceful) is stopped with SIGTERM, so uvicorn finishes its requests, and exits non-zero afterwards;
    the node agent has nothing to finish and exits at once. Containers keep running: Docker owns them."""
    def go():
        if graceful:
            atexit.register(os._exit, RESTART_EXIT_CODE)
            os.kill(os.getpid(), signal.SIGTERM)
        else:
            os._exit(RESTART_EXIT_CODE)
    timer = threading.Timer(delay, go)
    timer.daemon = True
    timer.start()
