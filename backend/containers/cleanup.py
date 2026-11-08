import logging
import threading
from typing import Optional
from containers.client import _get_client
from containers.locks import _build_locks, _build_locks_guard
from containers.naming import image_name

log = logging.getLogger("cloudbase.docker")


# ── Disk cleanup ──────────────────────────────────────────────────────────────
# Every rebuild retags `:latest`, so the previous image is left behind untagged
# ("dangling"), and BuildKit keeps a build cache that only grows. Neither is
# needed to run or restart an app: apps start from their tagged image, which is
# never touched here. Volumes (database data) are never touched either.

_cleanup_lock = threading.Lock()


BUILD_CACHE_KEEP_BYTES = 5 * 1024 ** 3          # routine: cache above this is evicted, oldest first


BUILD_CACHE_KEEP_BYTES_LOW_DISK = 1 * 1024 ** 3  # disk (almost) full


CLEANUP_PRE_BUILD_DISK_PERCENT = 85.0            # clean first when a build starts on a fuller disk


def disk_percent() -> Optional[float]:
    try:
        import psutil
        return float(psutil.disk_usage("/").percent)
    except Exception:
        return None


def _fmt_bytes(n: int) -> str:
    size = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def cleanup_docker(aggressive: bool = False, own_app_id: Optional[int] = None) -> dict:
    """Remove superseded images and trim the build cache. Returns a summary.

    Routine: untagged images older than a day, build cache above 5 GB.
    aggressive (disk nearly full): all untagged images, build cache above 1 GB.
    Skipped while another app is building, so a running build never loses layers.
    """
    if not _cleanup_lock.acquire(blocking=False):
        return {"skipped": "A cleanup is already running"}
    try:
        with _build_locks_guard:
            busy = [a for a, lk in _build_locks.items() if lk.locked() and a != own_app_id]
        if busy:
            return {"skipped": "A build is in progress"}

        client = _get_client()
        disk_before = disk_percent()
        result = {"images_removed": 0, "images_bytes": 0, "cache_bytes": 0, "errors": []}

        try:
            filters = {"dangling": True}
            if not aggressive:
                filters["until"] = "24h"
            pruned = client.images.prune(filters=filters)
            result["images_removed"] = len(pruned.get("ImagesDeleted") or [])
            result["images_bytes"] = int(pruned.get("SpaceReclaimed") or 0)
        except Exception as e:
            result["errors"].append(f"images: {e}")

        try:
            keep = BUILD_CACHE_KEEP_BYTES_LOW_DISK if aggressive else BUILD_CACHE_KEEP_BYTES
            try:
                pruned = client.api.prune_builds(keep_storage=keep)
            except TypeError:  # older docker-py without keep_storage
                pruned = client.api.prune_builds()
            result["cache_bytes"] = int(pruned.get("SpaceReclaimed") or 0)
        except Exception as e:
            result["errors"].append(f"build cache: {e}")

        result["disk_percent_before"] = disk_before
        result["disk_percent_after"] = disk_percent()
        result["summary"] = (
            f"Removed {result['images_removed']} old image layer(s) "
            f"({_fmt_bytes(result['images_bytes'])}) and {_fmt_bytes(result['cache_bytes'])} of build cache"
        )
        log.info("[docker] Cleanup%s: %s", " (low disk)" if aggressive else "", result["summary"])
        return result
    finally:
        _cleanup_lock.release()


def _cleanup_before_build(app_id: int, push_line_fn) -> None:
    """Free space first when the disk is nearly full, so the build doesn't fail on it."""
    try:
        pct = disk_percent()
        if pct is None or pct < CLEANUP_PRE_BUILD_DISK_PERCENT:
            return
        push_line_fn(app_id, f"[Docker] Disk is {pct:.0f}% full, cleaning up old images and build cache first…")
        res = cleanup_docker(aggressive=True, own_app_id=app_id)
        push_line_fn(app_id, f"[Docker] {res.get('summary') or res.get('skipped') or 'Cleanup done'}.")
    except Exception as e:
        log.warning("[docker] Pre-build cleanup failed: %s", e)


def remove_image(app_id: int) -> None:
    client = _get_client()
    img = image_name(app_id)
    try:
        client.images.remove(img, force=True)
        log.info("[docker] Removed image %s", img)
    except Exception:
        pass
