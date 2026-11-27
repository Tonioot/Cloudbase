"""The disk Docker has for itself on a node, and whether a container can be held to a size on it.

install.sh gives a new tenant node a file of its own for Docker (/var/lib/cloudbase-containers.img), an XFS file system
mounted with project quotas on Docker's folder. Containers, images and the build cache share it. With the overlay2
driver Docker can then hold each container to a size (--storage-opt size=…, containers/hardening.py): a ceiling, not
a reservation. Anywhere else there is no such limit, and containers start without one.

Needs only psutil, so the node agent can use it without loading any of the panel."""
import logging
import os
from typing import Callable, Optional

import psutil

log = logging.getLogger("cloudbase.docker")

ROOT = "/var/lib/docker"
IMAGE = "/var/lib/cloudbase-containers.img"

_supported: Optional[bool] = None


def _mount(path: str):
    """The mounted file system `path` is on: the one with the longest mount point above it."""
    found = None
    for part in psutil.disk_partitions(all=True):
        point = part.mountpoint.rstrip("/") or "/"
        if (path == point or path.startswith(point.rstrip("/") + "/")) and (found is None or len(point) > len(found.mountpoint)):
            found = part
    return found


def limit_supported(docker_info: Callable[[], dict]) -> bool:
    """Can a container be held to a size here: overlay2 on XFS, mounted with project quotas. Asked once.
    docker_info: what `docker info` answers."""
    global _supported
    if _supported is None:
        try:
            info = docker_info()
            backing = dict(tuple(pair) for pair in info.get("DriverStatus") or []).get("Backing Filesystem")
            part = _mount(info.get("DockerRootDir") or ROOT)
            options = set((part.opts if part else "").split(","))
            _supported = info.get("Driver") == "overlay2" and backing == "xfs" and bool(options & {"prjquota", "pquota"})
        except Exception as e:
            log.warning("[disk] could not find out whether containers can be limited in size: %s", e)
            return False
        if not _supported:
            log.warning("[disk] Containers get no disk limit on this server: Docker's folder is not on XFS with project "
                        "quotas (a new node gets that from install.sh)")
    return _supported


def usage() -> Optional[dict]:
    """{"gb", "percent", "own_file"} of Docker's own file system; None when Docker has none of its own. own_file: it
    is the image file on this machine's disk, so that much of the disk is spoken for."""
    try:
        if not os.path.ismount(ROOT):
            return None
        used = psutil.disk_usage(ROOT)
        return {"gb": round(used.total / 1024 ** 3, 1), "percent": used.percent, "own_file": os.path.exists(IMAGE)}
    except OSError:
        return None
