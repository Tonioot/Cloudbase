"""Client for the root helper (scripts/cloudbase-wireguard), reached through sudo.

Cloudbase does not run as root, and configuring a network interface needs root. The helper does only
that, with validated arguments; this module is the one place that calls it.
"""
import json
import os
import subprocess

HELPER = "/usr/local/lib/cloudbase/wireguard"
TIMEOUT = 40


class HelperError(RuntimeError):
    pass


def _command() -> list[str]:
    override = os.environ.get("CLOUDBASE_WG_HELPER")        # tests point this at a fake
    return override.split() if override else ["sudo", "-n", HELPER]


def _run(args: list[str], stdin: str | None = None) -> str:
    try:
        proc = subprocess.run([*_command(), *args], input=stdin, capture_output=True, text=True, timeout=TIMEOUT)
    except FileNotFoundError:
        raise HelperError("the WireGuard helper is not installed (run: cloudbase wireguard)")
    except subprocess.TimeoutExpired:
        raise HelperError("the WireGuard helper took too long")
    if proc.returncode != 0:
        raise HelperError((proc.stderr or proc.stdout).strip()[:300] or "the WireGuard helper failed")
    return proc.stdout.strip()


def keygen() -> str:
    return _run(["keygen"])


def start_hub(address: str, port: int) -> str:
    """Bring up the hub interface; returns its public key."""
    return json.loads(_run(["hub", address, str(port)]))["public_key"]


def start_node(address: str, hub_key: str, endpoint: str, allowed: str) -> str:
    """Bring up a node's interface towards the hub; returns the node's public key."""
    return json.loads(_run(["node", address, hub_key, endpoint, allowed]))["public_key"]


def set_peers(peers: list[dict]) -> int:
    return json.loads(_run(["peers"], stdin=json.dumps(peers)))["peers"]


def isolate_apps() -> None:
    """Containers of apps may not start connections to this machine itself (see the helper)."""
    _run(["isolate"])


def status() -> dict:
    return json.loads(_run(["status"]))
