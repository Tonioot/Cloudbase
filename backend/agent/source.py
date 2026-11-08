"""Getting an app's source onto this node (from the primary), and from there its Docker image (containers/prepare.py)."""
import os
import asyncio
from typing import Callable
import httpx
from containers import prepare, processes as pm
from agent.log import _agent_log


def _local_app_dir(app_id: int) -> str:
    return pm.get_app_dir(app_id)


async def _download_and_extract_source(client: httpx.AsyncClient, state, main_id: int) -> str:
    """Download source archive from primary, extract to local app dir. Returns app_dir."""
    import tarfile, io, shutil
    app_dir = _local_app_dir(main_id)

    main_base = state.main_url.rstrip("/")
    url = f"{main_base}/api/apps/{main_id}/source-archive"
    external_headers = {"X-Node-Token": state.auth_token}
    _agent_log(f"[source] Downloading the source of app {main_id} from primary")
    resp = await client.get(url, headers=external_headers, timeout=300)
    if resp.status_code != 200:
        # Say why: the primary explains itself in its answer, a bare "500" in the log does not
        try:
            reason = resp.json().get("detail") or resp.text
        except Exception:
            reason = resp.text
        raise RuntimeError(f"the primary could not give the source of app {main_id} (HTTP {resp.status_code}: {str(reason)[:300]})")

    # Extract to a temp dir first, then atomically replace to avoid partial states
    tmp_dir = app_dir + ".tmp"
    if os.path.exists(tmp_dir):
        await asyncio.to_thread(shutil.rmtree, tmp_dir)
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

    def _swap() -> None:
        if os.path.exists(app_dir):
            shutil.rmtree(app_dir)
        os.rename(tmp_dir, app_dir)
    await asyncio.to_thread(_swap)

    _agent_log(f"[source] Extracted {len(resp.content) // 1024}KB → {app_dir}")
    return app_dir


async def ensure_image(client: httpx.AsyncClient, state, spec: dict, progress: Callable[[str], None]) -> None:
    """Make sure this node has an image for the spec; the source comes from the primary when it has to be built."""
    async def fetch(app_id: int) -> str:
        progress("downloading")
        return await _download_and_extract_source(client, state, app_id)

    await prepare.ensure_image(spec, progress, fetch)
