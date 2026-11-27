import asyncio
import logging
import os
import re
import shutil
import threading
import time
from typing import Optional
from containers.client import _get_client, is_docker_available
from containers.locks import _build_locks, _build_locks_guard
from containers.naming import image_name
from containers import disk_area, processes as pm

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
        area = disk_area.usage()
        return max(float(psutil.disk_usage("/").percent), float(area["percent"]) if area else 0.0)
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


_APP_IMAGE = re.compile(r"^cloudbase/app-(\d+):")
_APP_FOLDER = re.compile(r"^app-(\d+)(\.tmp)?$")


def remove_leftovers(keep: set, up_to: int) -> dict:
    """Remove the images and sources of apps that no longer exist: ids up to `up_to` that are not in `keep`. An image
    a container still uses is left for the next time. {"images": n, "sources": n} removed."""
    def gone(app_id: int) -> bool:
        lock = _build_locks.get(app_id)
        return app_id <= up_to and app_id not in keep and not (lock and lock.locked())

    removed = {"images": 0, "sources": 0}
    try:
        client = _get_client()
        for image in client.images.list():
            for tag in image.tags:
                found = _APP_IMAGE.match(tag)
                if found and gone(int(found.group(1))):
                    try:
                        client.images.remove(tag)           # not forced: never from under a container
                        removed["images"] += 1
                    except Exception as e:
                        log.debug("[docker] %s stays for now: %s", tag, e)
    except Exception as e:
        log.debug("[docker] could not look for the images of deleted apps: %s", e)
    try:
        names = os.listdir(pm.APPS_BASE_DIR)
    except OSError:
        names = []
    for name in names:
        found = _APP_FOLDER.match(name)
        if found and gone(int(found.group(1))):
            shutil.rmtree(os.path.join(pm.APPS_BASE_DIR, name), ignore_errors=True)
            removed["sources"] += 1
    if any(removed.values()):
        log.info("[docker] Deleted apps: removed %d image(s) and %d source folder(s)", removed["images"], removed["sources"])
    return removed


# ── Keeping the disk clean on its own ─────────────────────────────────────────
CLEANUP_EVERY_SECONDS = 86400          # about once a day,
CLEANUP_FULL_DISK_EVERY_SECONDS = 3600  # at most hourly when the disk is 80% full,
CLEANUP_HIGH_PERCENT, CLEANUP_AGGRESSIVE_PERCENT = 80, 90   # and with the aggressive settings from 90%


def cleanup_due(disk: Optional[float], since_last: float) -> Optional[bool]:
    """None when it is not time yet; otherwise whether to clean aggressively."""
    high = disk is not None and disk >= CLEANUP_HIGH_PERCENT
    if since_last < (CLEANUP_FULL_DISK_EVERY_SECONDS if high else CLEANUP_EVERY_SECONDS):
        return None
    return disk is not None and disk >= CLEANUP_AGGRESSIVE_PERCENT


async def cleanup_forever(first_delay: float = 600, check_every: float = 900) -> None:
    """Keep this machine's disk from filling up with old images and build cache. The primary and every node's
    agent run this for themselves: each knows its own disk best, and nobody has to ask."""
    await asyncio.sleep(first_delay)
    last = float("-inf")
    while True:
        try:
            if await asyncio.to_thread(is_docker_available):
                aggressive = cleanup_due(await asyncio.to_thread(disk_percent), time.monotonic() - last)
                if aggressive is not None:
                    last = time.monotonic()
                    result = await asyncio.to_thread(cleanup_docker, aggressive)
                    log.info("[docker-cleanup] %s", result.get("summary") or result.get("errors") or result)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("[docker-cleanup] error: %s", e)
        await asyncio.sleep(check_every)
