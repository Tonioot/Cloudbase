"""Restart this server, through the root helper (scripts/cloudbase-power) since Cloudbase does not run as root."""
import os
import subprocess

HELPER = "/usr/local/lib/cloudbase/power"


def _helper(*args: str) -> subprocess.CompletedProcess | None:
    override = os.environ.get("CLOUDBASE_POWER_HELPER")        # tests point this at a fake
    command = override.split() if override else ["sudo", "-n", HELPER]
    try:
        return subprocess.run([*command, *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None


def reboot() -> dict:
    """Restart this server in a few seconds. {"status": "restarting" | "error", "message": ...}"""
    r = _helper("reboot")
    if r is None or r.returncode != 0:
        why = ((r.stderr or r.stdout).strip()[-200:] if r else "") or "the restart helper is not installed"
        return {"status": "error", "message": f"This server could not be restarted: {why}. Run on it once: cloudbase update"}
    return {"status": "restarting", "message": "The server is restarting"}
