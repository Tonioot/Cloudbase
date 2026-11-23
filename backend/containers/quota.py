"""Client for the root helper scripts/cloudbase-quota, reached through sudo: the disks of databases with a disk limit,
and the counters of what app containers send to the internet. This module is the one place that calls it.
"""
import json
import os
import subprocess
from typing import Optional

HELPER = "/usr/local/lib/cloudbase/quota"
DISK_DIR = "/var/lib/cloudbase-disks"          # the same as in the helper
TIMEOUT = 900                                  # making or growing a large disk writes its metadata


class QuotaError(RuntimeError):
    pass


def _command() -> list[str]:
    override = os.environ.get("CLOUDBASE_QUOTA_HELPER")      # tests point this at a fake
    return override.split() if override else ["sudo", "-n", HELPER]


def _run(args: list[str], stdin: Optional[str] = None, timeout: float = TIMEOUT) -> dict:
    try:
        proc = subprocess.run([*_command(), *args], input=stdin, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise QuotaError("the disk and traffic helper is not installed (run: cloudbase quota)")
    except subprocess.TimeoutExpired:
        raise QuotaError("the disk and traffic helper took too long")
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout).strip()[:300]
        if "a password is required" in message or "not allowed" in message:
            message = "the disk and traffic helper is not installed (run: cloudbase quota)"
        raise QuotaError(message or "the disk and traffic helper failed")
    return json.loads(proc.stdout or "{}")


# ── disks ─────────────────────────────────────────────────────────────────────
def image_path(database_id: int) -> str:
    return os.path.join(DISK_DIR, f"db-{int(database_id)}.img")


def data_path(database_id: int) -> str:
    """What the database's Docker volume binds to: only there while its disk is mounted."""
    return os.path.join(DISK_DIR, f"db-{int(database_id)}", "data")


def has_disk(database_id: int) -> bool:
    return os.path.exists(image_path(database_id))


def is_mounted(database_id: int) -> bool:
    return os.path.isdir(data_path(database_id))     # the data directory is on the disk, so only there once mounted


def image_size_mb(database_id: int) -> int:
    """The size the disk was made or grown to (its file system is a little smaller)."""
    return os.path.getsize(image_path(database_id)) // (1024 * 1024)


def create_disk(database_id: int, size_mb: int) -> dict:
    return _run(["disk-create", str(int(database_id)), str(int(size_mb))])


def mount_disk(database_id: int) -> dict:
    return _run(["disk-mount", str(int(database_id))])


def grow_disk(database_id: int, size_mb: int) -> dict:
    return _run(["disk-grow", str(int(database_id)), str(int(size_mb))])


def remove_disk(database_id: int) -> dict:
    return _run(["disk-remove", str(int(database_id))])


def disk_usage(database_id: int) -> Optional[dict]:
    """{"disk_size_mb", "disk_used_mb"} of a mounted disk, read without the helper; None when there is none."""
    path = os.path.dirname(data_path(database_id))
    if not is_mounted(database_id):
        return None
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    mb = 1024 * 1024
    return {"disk_size_mb": round(st.f_blocks * st.f_frsize / mb, 1),
            "disk_used_mb": round((st.f_blocks - st.f_bfree) * st.f_frsize / mb, 1)}


# ── traffic ───────────────────────────────────────────────────────────────────
def traffic(ips: list[str]) -> dict[str, int]:
    """{ip: bytes} sent to public addresses since the last call by the addresses counted until now; from here on it
    counts `ips`."""
    result = _run(["traffic"], stdin=json.dumps({"ips": sorted(set(ips))}), timeout=60)
    return {str(ip): int(n) for ip, n in (result.get("bytes") or {}).items()}


# ── tenant mode ───────────────────────────────────────────────────────────────
def tenant_firewall(on: bool) -> dict:
    """No mail out of apps and builds; builds reach neither this machine nor private addresses (core/tenant.py)."""
    return _run(["tenant", "on" if on else "off"], timeout=60)
