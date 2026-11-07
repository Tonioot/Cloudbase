"""Getting an app's source and its Docker image onto this node."""
import os
import asyncio
import platform
import subprocess
from typing import Callable
import httpx
from containers import naming as containers_naming, build as containers_build, processes as pm
from agent.log import _agent_log

# (app id, revision) pairs built since this agent started: a node that cannot reach a newer
# revision than the one it was asked for must not rebuild the same thing over and over
_built: set[tuple[int, str]] = set()
_build_lock = asyncio.Lock()

def _local_app_dir(app_name: str) -> str:
    # Keep source extraction path aligned with backend deploy path derivation.
    # This avoids case-mismatch failures like "inside" vs "Inside".
    try:
        return pm.get_app_dir(app_name)
    except Exception:
        safe = app_name.lower().replace(" ", "-")
        return os.path.join(os.path.expanduser("~/.cloudbase/apps"), safe)


def _local_image_ok(img: str) -> bool:
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


async def _download_and_extract_source(client: httpx.AsyncClient, state, main_id: int, app_name: str) -> str:
    """Download source archive from primary, extract to local app dir. Returns app_dir."""
    import tarfile, io, shutil
    app_dir = _local_app_dir(app_name)

    main_base = state.main_url.rstrip("/")
    url = f"{main_base}/api/apps/{main_id}/source-archive"
    external_headers = {"X-Node-Token": state.auth_token}
    _agent_log(f"[source] Downloading source for '{app_name}' from primary")
    resp = await client.get(url, headers=external_headers, timeout=300)
    resp.raise_for_status()

    # Extract to a temp dir first, then atomically replace to avoid partial states
    tmp_dir = app_dir + ".tmp"
    if os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir)
    os.makedirs(tmp_dir, exist_ok=True)

    def _extract(data: bytes) -> None:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            # Never write outside tmp_dir: no absolute paths, ../ or links out of it
            if hasattr(tarfile, "data_filter"):
                tar.extractall(path=tmp_dir, filter="data")
            else:  # Python without extraction filters
                root = os.path.realpath(tmp_dir)
                for m in tar.getmembers():
                    dest = os.path.realpath(os.path.join(root, m.name))
                    targets = [dest]
                    if m.issym():
                        targets.append(os.path.realpath(os.path.join(os.path.dirname(dest), m.linkname)))
                    elif m.islnk():
                        targets.append(os.path.realpath(os.path.join(root, m.linkname)))
                    if m.isdev() or any(os.path.commonpath([root, t]) != root for t in targets):
                        raise RuntimeError(f"Unsafe path in source archive: {m.name}")
                tar.extractall(path=tmp_dir)

    await asyncio.to_thread(_extract, resp.content)

    if os.path.exists(app_dir):
        shutil.rmtree(app_dir)
    os.rename(tmp_dir, app_dir)

    _agent_log(f"[source] Extracted {len(resp.content) // 1024}KB → {app_dir}")
    return app_dir


async def _build_image_local(local_id: int, app_name: str, app_dir: str, payload: dict) -> str:
    """Build Docker image natively using dm.build_image (handles Dockerfile generation).
    Returns image name."""

    app_type = (payload.get("app_type") or "").strip().lower()
    start_cmd = payload.get("start_command") or ""
    port = payload.get("internal_port", payload.get("port"))
    port = 8000 if port is None else port  # 0 = no port (worker, cron job)

    if not app_type or app_type == "unknown":
        app_type = pm.detect_app_type_from_command(start_cmd) if start_cmd else "unknown"

    img = containers_naming.image_name(local_id, app_name)

    _agent_log(f"[build] Building image {img} for {platform.machine()} in {app_dir}")

    def _push(aid, line):
        _agent_log(f"[build] {line}")

    await asyncio.to_thread(
        containers_build.build_image,
        local_id, app_name, app_dir, _push,
        app_type, start_cmd, port,
        build_command=payload.get("build_command") or "",
        build_env=payload.get("env_vars") or {},
        # Label the image so the next start sees it is current and skips the
        # download + rebuild (_ensure_replica_app_deployed compares this label).
        source_revision=payload.get("source_revision") or None,
    )
    _agent_log(f"[build] Image {img} ready")
    return img


async def ensure_image(client: httpx.AsyncClient, state, spec: dict, progress: Callable[[str], None]) -> None:
    """Make sure this node has an image for the spec: only downloads and builds when it is missing or at another revision.

    `progress` is told what is happening ("downloading", "building_image")."""
    main_id = int(spec["app_id"])
    app_name = spec.get("app_name") or spec.get("name") or ""
    if not app_name:
        raise RuntimeError("Missing app_name")
    desired_revision = spec.get("source_revision")

    img = containers_naming.image_name(main_id, app_name)
    if _local_image_ok(img):
        if not desired_revision:
            return
        try:
            r = subprocess.run(
                ["docker", "inspect", "--format", "{{index .Config.Labels \"cloudbase.source_revision\"}}", img],
                capture_output=True, text=True, timeout=10,
            )
            image_revision = r.stdout.strip()
        except Exception:
            image_revision = ""
        if image_revision == desired_revision:
            _agent_log(f"[deploy] Image {img} already at revision {desired_revision}, skipping build")
            return
        if (main_id, desired_revision) in _built:
            return              # built for it before; the source on the primary has moved on since
        _agent_log(f"[deploy] Image {img} stale ({image_revision!r} != {desired_revision!r}), rebuilding")

    async with _build_lock:     # one build at a time: a Raspberry Pi cannot do two
        progress("downloading")
        app_dir = await _download_and_extract_source(client, state, main_id, app_name)
        progress("building_image")
        await _build_image_local(main_id, app_name, app_dir, spec)
    if desired_revision:
        _built.add((main_id, desired_revision))
