"""Getting the Docker image of an app onto this machine: only builds when it is missing or at another revision.

Used by the agent on a node (which first downloads the source from the primary) and by the primary for its own
instances (the source is already there). What differs is only where the source comes from: `fetch`.
"""
import asyncio
import logging
import platform
import subprocess
from typing import Awaitable, Callable

from containers import legacy_names, naming as containers_naming, build as containers_build, processes as pm

_logger = logging.getLogger("cloudbase.agent")


def _log(message: str) -> None:
    _logger.info(message)


# Builds since this process started: a machine that cannot reach a newer revision than the one it was asked for
# must not rebuild the same thing over and over
_built: set[tuple[int, str]] = set()      # (app id, build key)
_build_lock = asyncio.Lock()


def local_image_ok(img: str) -> bool:
    """Return True if the image exists locally and matches this node's CPU architecture."""
    host = platform.machine().lower()
    expected = "arm64" if ("aarch64" in host or "arm64" in host) else "amd64"
    try:
        r = subprocess.run(
            ["docker", "inspect", "--format", "{{.Architecture}}", img],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return False
        arch = r.stdout.strip()
        # Docker normalises x86_64 → amd64, but be lenient in both directions
        if expected == "amd64":
            return arch in ("amd64", "x86_64")
        return arch == expected
    except Exception:
        return False


def build_arguments(spec: dict) -> tuple:
    """What build_image/build_key take, from a spec: one place, so the key checked is the key built."""
    app_type = (spec.get("app_type") or "").strip().lower()
    start_cmd = spec.get("start_command") or ""
    port = spec.get("internal_port", spec.get("port"))
    port = 8000 if port is None else port  # 0 = no port (worker, cron job)
    if not app_type or app_type == "unknown":
        app_type = pm.detect_app_type_from_command(start_cmd) if start_cmd else "unknown"
    return app_type, start_cmd, port, spec.get("build_command") or "", spec.get("env_vars") or {}, spec.get("source_revision") or None


async def build_image_local(local_id: int, app_dir: str, spec: dict, push=None) -> str:
    """Build the Docker image on this node (build_image generates the Dockerfile). Returns the image name.

    `push(app_id, line)` also gets the build output; it always goes to the agent log."""
    app_type, start_cmd, port, build_command, build_env, revision = build_arguments(spec)
    img = containers_naming.image_name(local_id)
    _log(f"[build] Building image {img} for {platform.machine()} in {app_dir}")

    def _push(aid, line):
        _log(f"[build] {line}")
        if push:
            push(aid, line)

    await asyncio.to_thread(
        containers_build.build_image,
        local_id, app_dir, _push,
        app_type, start_cmd, port,
        build_command=build_command, build_env=build_env,
        source_revision=revision,
    )
    _log(f"[build] Image {img} ready")
    return img


async def ensure_image(spec: dict, progress: Callable[[str], None],
                       fetch: Callable[[int], Awaitable[str]], push=None) -> None:
    """Make sure this machine has an image for the spec.

    `progress` is told what is happening ("building_image"; `fetch` reports "downloading" itself).
    `fetch(app_id)` gives the directory with the source. `push(app_id, line)` also gets the build output."""
    main_id = int(spec["app_id"])
    if spec.get("app_name"):
        await asyncio.to_thread(legacy_names.adopt, main_id, spec["app_name"])      # ONE-OFF (delete with legacy_names.py)
    app_type, start_cmd, port, build_command, build_env, revision = build_arguments(spec)
    key = containers_build.build_key(app_type, start_cmd, port, build_command, build_env, revision)

    img = containers_naming.image_name(main_id)
    if await asyncio.to_thread(local_image_ok, img):
        if not revision:
            return                  # nothing says what the image was made from: use it, as before
        if containers_build.image_key(main_id) == key:
            _log(f"[deploy] Image {img} is current (revision {revision[:8]}, same settings), skipping build")
            return
        if (main_id, key) in _built:
            return              # built for this before; the source has moved on since
        _log(f"[deploy] Image {img} is not current, rebuilding")

    async with _build_lock:     # one build at a time: a Raspberry Pi cannot do two
        app_dir = await fetch(main_id)
        progress("building_image")
        await build_image_local(main_id, app_dir, spec, push)
    if key:
        _built.add((main_id, key))
