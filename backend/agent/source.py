import os
import time
import asyncio
import platform
import subprocess
from typing import Optional
import httpx
from containers import naming as containers_naming, build as containers_build, processes as pm
from agent.log import _agent_log
from agent.urls import _LOCAL_API_BASE

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


async def _download_and_extract_source(client: httpx.AsyncClient, state, main_id: int, app_name: str, headers) -> str:
    """Download source archive from primary, extract to local app dir. Returns app_dir."""
    import tarfile, io, shutil
    app_dir = _local_app_dir(app_name)

    main_base = state.main_url.rstrip("/")
    url = f"{main_base}/api/apps/{main_id}/source-archive"
    # Use X-Node-Token for requests to the external primary — X-Agent-Token only
    # works on the local loopback API (127.0.0.1).
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


async def _report_replica_substatus(client: httpx.AsyncClient, state, main_id: int, replica_id: int, substatus: Optional[str]) -> None:
    """Fire-and-forget: update substatus on the main server so the UI can show startup progress."""
    try:
        await client.patch(
            f"{state.main_url}/api/apps/{main_id}/replicas/{replica_id}/substatus",
            json={"substatus": substatus},
            headers={"X-Node-Token": state.auth_token},
            timeout=5,
        )
    except Exception as e:
        _agent_log(f"[agent] substatus report failed (replica={replica_id} substatus={substatus}): {e}")


async def _ensure_replica_app_deployed(client: httpx.AsyncClient, state, main_id, payload, headers, replica_id: Optional[int] = None) -> int:
    """Ensure source + image are ready on this node. Only downloads/builds when needed.
    Returns main_id (used directly as the image app_id — no local DB needed)."""

    app_name = payload.get("app_name") or payload.get("name") or ""
    if not app_name:
        raise RuntimeError("Missing app_name")
    desired_revision = payload.get("source_revision")
    desired_start_command = payload.get("start_command") or ""
    desired_app_type = (payload.get("app_type") or "").strip().lower()
    if not desired_app_type or desired_app_type == "unknown":
        desired_app_type = pm.detect_app_type_from_command(desired_start_command) if desired_start_command else "unknown"

    # Check if we already have a valid image built under main_id
    img = containers_naming.image_name(int(main_id), app_name)
    if _local_image_ok(img):
        if not desired_revision:
            _agent_log(f"[deploy] Image {img} already present, skipping build")
            return int(main_id)
        # Check revision via docker inspect label
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
            return int(main_id)
        _agent_log(f"[deploy] Image {img} stale ({image_revision!r} != {desired_revision!r}), rebuilding")

    # Image missing or stale — report substatus and build
    if replica_id is not None:
        await _report_replica_substatus(client, state, int(main_id), replica_id, "downloading")
    app_dir = await _download_and_extract_source(client, state, int(main_id), app_name, headers)
    if replica_id is not None:
        await _report_replica_substatus(client, state, int(main_id), replica_id, "building_image")
    await _build_image_local(int(main_id), app_name, app_dir, payload)
    return int(main_id)


async def _wait_local_api(client, timeout: float = 90) -> None:
    """After a node reboot the agent can be up before the local Cloudbase API."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            r = await client.get(f"{_LOCAL_API_BASE}/api/health", timeout=3)
            if r.status_code < 500:
                return
        except Exception:
            pass
        if time.monotonic() > deadline:
            raise RuntimeError("Local Cloudbase API did not come up in time")
        await asyncio.sleep(2)
