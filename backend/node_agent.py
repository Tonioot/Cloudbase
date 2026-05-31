import os
import json
import time
import socket
import base64
import secrets
import asyncio
import argparse
import platform
import subprocess
import logging
from typing import Optional, Any, Tuple, Dict, Callable
from dataclasses import dataclass

import httpx
import websockets
import psutil

# Configuration
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(_BASE_DIR, "agent_state.json")
AGENT_LOG_FILE = os.path.expanduser("~/.cloudbase/logs/node-agent.log")
_LOCAL_API_BASE = "http://127.0.0.1:7823"

os.makedirs(os.path.dirname(AGENT_LOG_FILE), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(AGENT_LOG_FILE), logging.StreamHandler()],
)

def _agent_log(message: str) -> None:
    logging.info(message)

@dataclass
class AgentState:
    main_url: str
    auth_token: str
    node_id: int
    node_name: str
    heartbeat_interval: int = 15

def _normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url.startswith("http"):
        url = "http://" + url
    return url

def _ws_url(main_url: str) -> str:
    url = _normalize_url(main_url)
    if url.startswith("https"):
        return url.replace("https://", "wss://") + "/api/nodes/ws/agent"
    return url.replace("http://", "ws://") + "/api/nodes/ws/agent"

def _local_ws_url(path: str) -> str:
    # Assuming local Cloudbase runs on 7823
    return f"ws://127.0.0.1:7823{path}"

def _load_agent_token() -> Optional[str]:
    token = os.environ.get("AGENT_TOKEN")
    if token:
        return token
    token_file = os.path.expanduser("~/.cloudbase/agent_token")
    if os.path.exists(token_file):
        with open(token_file) as f:
            tok = f.read().strip()
        if tok:
            return tok
    return None

def _load_state() -> Optional[AgentState]:
    if not os.path.exists(STATE_PATH):
        return None
    try:
        with open(STATE_PATH, "r") as f:
            data = json.load(f)
        # Strip unknown fields so old state files with removed fields (e.g. app_id_map) still load
        known = {f for f in AgentState.__dataclass_fields__}
        data = {k: v for k, v in data.items() if k in known}
        return AgentState(**data)
    except Exception as e:
        _agent_log(f"[agent] Error loading state: {e}")
        return None

def _save_state(state: AgentState) -> None:
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(state.__dict__, f)
    except Exception as e:
        _agent_log(f"[agent] Error saving state: {e}")

def _collect_node_metrics() -> dict:
    try:
        return {
            "cpu_percent": psutil.cpu_percent(interval=None),
            "memory_percent": psutil.virtual_memory().percent,
            "disk_percent": psutil.disk_usage("/").percent,
        }
    except Exception:
        return {}

def _collect_system_info() -> dict:
    import socket as _socket
    info: dict = {}
    try:
        info["hostname"]  = _socket.gethostname()
        info["os"]        = platform.platform()
        info["os_short"]  = f"{platform.system()} {platform.release()}"
        info["arch"]      = platform.machine()
        
        try:
            info["uptime_secs"] = round(time.time() - psutil.boot_time())
        except Exception: pass

        mem  = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        info["ram_total_mb"]  = round(mem.total / 1024 / 1024)
        info["disk_total_gb"] = round(disk.total / 1024 / 1024 / 1024, 1)
        
        info["cpu_count"]         = psutil.cpu_count(logical=False) or 0
        info["cpu_count_logical"] = psutil.cpu_count(logical=True) or 0
        
        try:
            freq = psutil.cpu_freq()
            if freq:
                info["cpu_freq_mhz"] = round(freq.max or freq.current)
        except Exception: pass

        # Try to get CPU model
        try:
            import cpuinfo
            c = cpuinfo.get_cpu_info()
            info["cpu_model"] = c.get("brand_raw", "")
        except Exception:
            if platform.system() == "Windows":
                try:
                    info["cpu_model"] = subprocess.check_output(["wmic", "cpu", "get", "name"]).decode().split("\n")[1].strip()
                except Exception: pass
            elif platform.system() == "Darwin":
                try:
                    info["cpu_model"] = subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"]).decode().strip()
                except Exception: pass
            else:
                try:
                    with open("/proc/cpuinfo") as f:
                        for line in f:
                            if line.startswith("model name"):
                                info["cpu_model"] = line.split(":", 1)[1].strip()
                                break
                except Exception: pass

        try:
            addrs = psutil.net_if_addrs()
            ips = []
            for iface, addr_list in addrs.items():
                if iface.startswith("lo") or "loopback" in iface.lower(): continue
                for a in addr_list:
                    if a.family == _socket.AF_INET: ips.append(a.address)
            if ips:
                info["ip"] = ips[0]
                info["ip_all"] = ips
        except Exception: pass
    except Exception:
        info.setdefault("hostname", _socket.gethostname())
    return info

def _build_capabilities() -> dict:
    return {
        "agent_version": "1.1.0",
        "features": ["streaming_logs", "streaming_stats", "file_management", "nginx_management", "hybrid_mode"],
        "platform": platform.system(),
        "arch": platform.machine(),
    }

async def _register(
    client: httpx.AsyncClient,
    main_url: str,
    invite_code: str,
    node_name: str,
    public_host: Optional[str] = None,
    heartbeat_interval: int = 15,
) -> AgentState:
    payload = {
        "invite_code": invite_code,
        "name": node_name,
        "public_host": public_host,
        "heartbeat_interval": heartbeat_interval,
        "capabilities": _build_capabilities(),
        "metadata_json": _collect_system_info(),
    }
    response = await client.post(f"{main_url}/api/nodes/agent/register", json=payload, timeout=20)
    response.raise_for_status()
    data = response.json()
    return AgentState(
        main_url=main_url,
        auth_token=data["auth_token"],
        node_id=data["node"]["id"],
        node_name=data["node"]["name"],
        heartbeat_interval=data["node"]["heartbeat_interval"],
    )

async def _report_result(
    client: httpx.AsyncClient,
    state: AgentState,
    command_id: int,
    *,
    status: str,
    result: Optional[dict[str, Any]] = None,
    error_message: Optional[str] = None,
) -> None:
    headers = {"X-Node-Token": state.auth_token}
    payload = {
        "status": status,
        "result": result,
        "error_message": error_message,
    }
    response = await client.post(
        f"{state.main_url}/api/nodes/agent/commands/{command_id}/result",
        json=payload,
        headers=headers,
        timeout=20,
    )
    response.raise_for_status()


# ─── Tunnel client state ──────────────────────────────────────────────────────
# replica_id → running asyncio.Task (the reconnecting tunnel loop)
_active_tunnels: dict[int, asyncio.Task] = {}


def _tunnel_ws_url(main_url: str, replica_id: int) -> str:
    url = _normalize_url(main_url)
    if url.startswith("https"):
        base = url.replace("https://", "wss://")
    else:
        base = url.replace("http://", "ws://")
    return f"{base}/api/nodes/ws/tunnel/{replica_id}"


async def _tunnel_relay(ws, local_port: int) -> None:
    """Relay TCP connections from the main node's TCP listener to the local replica.

    The main node sends:
      {"type": "connect", "conn_id": "..."}   — open TCP conn to 127.0.0.1:local_port
      {"type": "data",    "conn_id": "...", "data": "<base64>"}
      {"type": "close",   "conn_id": "..."}

    This side responds with:
      {"type": "data",   "conn_id": "...", "data": "<base64>"}
      {"type": "close",  "conn_id": "..."}
    """
    conns: dict[str, tuple[asyncio.StreamReader, asyncio.StreamWriter]] = {}

    async def _forward_to_ws(conn_id: str, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                await ws.send(json.dumps({
                    "type": "data",
                    "conn_id": conn_id,
                    "data": base64.b64encode(chunk).decode(),
                }))
        except Exception:
            pass
        finally:
            try:
                await ws.send(json.dumps({"type": "close", "conn_id": conn_id}))
            except Exception:
                pass
            pair = conns.pop(conn_id, None)
            if pair:
                try:
                    pair[1].close()
                except Exception:
                    pass

    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            mtype   = msg.get("type")
            conn_id = msg.get("conn_id")
            if not conn_id:
                continue

            if mtype == "connect":
                try:
                    reader, writer = await asyncio.open_connection("127.0.0.1", local_port)
                    conns[conn_id] = (reader, writer)
                    asyncio.get_running_loop().create_task(_forward_to_ws(conn_id, reader))
                except Exception as e:
                    _agent_log(f"[tunnel] connect to 127.0.0.1:{local_port} failed ({e})")
                    try:
                        await ws.send(json.dumps({"type": "close", "conn_id": conn_id}))
                    except Exception:
                        pass

            elif mtype == "data":
                pair = conns.get(conn_id)
                if pair:
                    _, writer = pair
                    try:
                        writer.write(base64.b64decode(msg["data"]))
                        await writer.drain()
                    except Exception:
                        conns.pop(conn_id, None)

            elif mtype == "close":
                pair = conns.pop(conn_id, None)
                if pair:
                    try:
                        pair[1].close()
                    except Exception:
                        pass
    finally:
        for _, writer in list(conns.values()):
            try:
                writer.close()
            except Exception:
                pass
        conns.clear()


async def _run_tunnel(state: AgentState, replica_id: int, local_port: int) -> None:
    """Persistent reconnecting tunnel loop for one replica.

    Connects to the main node's /api/nodes/ws/tunnel/{replica_id} and relays
    TCP connections until the task is cancelled.
    """
    # Brief delay so the command-result HTTP response arrives at the main node
    # before the tunnel WebSocket connects (avoids a DB race on replica.status).
    await asyncio.sleep(0.5)

    tunnel_url = _tunnel_ws_url(state.main_url, replica_id)
    while True:
        try:
            _agent_log(f"[tunnel] replica={replica_id} connecting to {tunnel_url}")
            async with websockets.connect(
                tunnel_url,
                additional_headers={"x-node-token": state.auth_token},
                ping_interval=20,
                ping_timeout=30,
                max_size=2 ** 22,
            ) as ws:
                _agent_log(f"[tunnel] replica={replica_id} connected, relaying to 127.0.0.1:{local_port}")
                await _tunnel_relay(ws, local_port)
        except asyncio.CancelledError:
            _agent_log(f"[tunnel] replica={replica_id} cancelled")
            break
        except Exception as e:
            _agent_log(f"[tunnel] replica={replica_id} disconnected ({type(e).__name__}: {e}), reconnecting in 5s")

        # Check if tunnel was explicitly removed before reconnecting
        if replica_id not in _active_tunnels:
            break
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            break


def _start_tunnel_task(state: AgentState, replica_id: int, local_port: int) -> None:
    """Register and start a persistent tunnel task for a replica."""
    old = _active_tunnels.pop(replica_id, None)
    if old and not old.done():
        old.cancel()
    task = asyncio.get_running_loop().create_task(
        _run_tunnel(state, replica_id, local_port)
    )
    _active_tunnels[replica_id] = task
    _agent_log(f"[tunnel] replica={replica_id} task started (port={local_port})")


async def _stop_tunnel_task(replica_id: int) -> None:
    """Cancel and await the tunnel task for a replica."""
    task = _active_tunnels.pop(replica_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=3.0)
        except Exception:
            pass
    _agent_log(f"[tunnel] replica={replica_id} task stopped")


# ─── Orphan cleanup ───────────────────────────────────────────────────────────

async def _cleanup_orphaned_replica_containers(client: httpx.AsyncClient, state: AgentState) -> None:
    """Stop any local Docker containers whose replica_id no longer exists on the primary.

    Container names follow the pattern  cloudbase-app-{app_id}-replica-{replica_id}.
    We ask the primary for the set of live replica IDs on this node, then kill every
    matching container whose replica_id is not in that set.
    """
    import re
    node_headers = {"X-Node-Token": state.auth_token}
    try:
        resp = await client.get(
            f"{state.main_url}/api/nodes/agent/my-replicas",
            headers=node_headers, timeout=10,
        )
        if resp.status_code != 200:
            return
        live_ids: set[int] = {r["id"] for r in resp.json().get("replicas", [])}

        # Enumerate local Docker containers whose name matches the replica pattern
        import subprocess, json as _json
        result = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}", "--filter", "name=cloudbase-app-"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return

        pattern = re.compile(r"^cloudbase-app-(\d+)-replica-(\d+)$")
        for cname in result.stdout.splitlines():
            m = pattern.match(cname.strip())
            if not m:
                continue
            replica_id = int(m.group(2))
            if replica_id not in live_ids:
                _agent_log(f"[cleanup] Stopping orphan replica container '{cname}' (replica_id={replica_id} no longer exists)")
                try:
                    subprocess.run(["docker", "rm", "-f", cname], capture_output=True, timeout=15)
                except Exception as e:
                    _agent_log(f"[cleanup] Failed to remove '{cname}': {e}")
    except Exception as e:
        _agent_log(f"[cleanup] Replica container orphan check failed: {e}")


# ─── Command Handlers ─────────────────────────────────────────────────────────


async def cmd_delete_app(_client, _state, main_id, _payload, _headers):
    """Stop all replica containers for this app on this node."""
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}", "--filter", f"name=cloudbase-app-{main_id}-replica-"],
            capture_output=True, text=True, timeout=10,
        )
        for cname in result.stdout.splitlines():
            cname = cname.strip()
            if cname:
                subprocess.run(["docker", "rm", "-f", cname], capture_output=True, timeout=15)
                _agent_log(f"[delete_app] Removed container '{cname}'")
    except Exception as e:
        _agent_log(f"[delete_app] Error stopping containers for app {main_id}: {e}")
    return {"message": f"Containers for app {main_id} removed"}


async def cmd_get_logs_tail(_client, _state, main_id, payload, _headers):
    """Get recent logs from all replica containers for this app."""
    limit = payload.get("limit") or 200
    lines: list[str] = []
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}", "--filter", f"name=cloudbase-app-{main_id}-replica-"],
            capture_output=True, text=True, timeout=10,
        )
        for cname in result.stdout.splitlines():
            cname = cname.strip()
            if not cname:
                continue
            r = subprocess.run(
                ["docker", "logs", "--tail", str(limit), cname],
                capture_output=True, text=True, timeout=10,
            )
            lines.extend((r.stdout + r.stderr).splitlines())
    except Exception as e:
        _agent_log(f"[get_logs_tail] Error: {e}")
    return {"lines": lines[-limit:]}


async def cmd_get_stats(client, state, main_id, payload, headers):
    """Aggregate stats across all replica containers for this app on this node."""
    resp = await client.get(
        f"{_LOCAL_API_BASE}/api/apps/{main_id}/replicas/aggregate-stats",
        headers=headers, timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


async def cmd_get_replica_stats(client, state, main_id, payload, headers):
    replica_id = payload.get("replica_id")
    if replica_id is None:
        return {"status": "stopped", "docker": True, "error": "missing replica_id"}
    resp = await client.get(
        f"{_LOCAL_API_BASE}/api/apps/{main_id}/replicas/{int(replica_id)}/stats-remote",
        headers=headers,
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()

async def cmd_get_agent_logs(client, state, main_id, payload, headers):
    log_file = os.path.expanduser("~/.cloudbase/logs/node-agent.log")
    lines = payload.get("lines") or 100
    if not os.path.exists(log_file):
        return {"lines": []}
    with open(log_file) as f:
        all_lines = f.readlines()
    return {"lines": [l.rstrip() for l in all_lines[-lines:]]}

async def cmd_get_replica_logs(client, state, main_id, payload, headers):
    container_name = payload.get("container_name")
    lines = payload.get("lines") or 200
    if not container_name:
        return {"lines": [], "error": "missing container_name"}
    try:
        import subprocess as _sp
        raw = await asyncio.to_thread(
            lambda: _sp.check_output(
                ["docker", "logs", "--tail", str(lines), container_name],
                stderr=_sp.STDOUT, text=True,
            )
        )
        return {"lines": raw.splitlines()}
    except Exception as e:
        return {"lines": [], "error": str(e)}




def _local_app_dir(app_name: str) -> str:
    # Keep source extraction path aligned with backend deploy path derivation.
    # This avoids case-mismatch failures like "inside" vs "Inside".
    try:
        import process_manager as pm
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
    # works on the local loopback API (127.0.0.1:7823).
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
    import docker_manager as dm  # import here to avoid circular at module level on remote nodes
    import process_manager as pm
    import re as _re

    app_type = (payload.get("app_type") or "").strip().lower()
    start_cmd = payload.get("start_command") or ""
    port = payload.get("internal_port") or payload.get("port") or 8000

    if not app_type or app_type == "unknown":
        app_type = pm.detect_app_type_from_command(start_cmd) if start_cmd else "unknown"

    img = dm.image_name(local_id, app_name)

    _agent_log(f"[build] Building image {img} for {platform.machine()} in {app_dir}")

    def _push(aid, line):
        _agent_log(f"[build] {line}")

    await asyncio.to_thread(
        dm.build_image,
        local_id, app_name, app_dir, _push,
        app_type, start_cmd, port,
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
    import docker_manager as dm
    import process_manager as pm

    app_name = payload.get("app_name") or payload.get("name") or ""
    if not app_name:
        raise RuntimeError("Missing app_name")
    desired_revision = payload.get("source_revision")
    desired_start_command = payload.get("start_command") or ""
    desired_app_type = (payload.get("app_type") or "").strip().lower()
    if not desired_app_type or desired_app_type == "unknown":
        desired_app_type = pm.detect_app_type_from_command(desired_start_command) if desired_start_command else "unknown"

    # Check if we already have a valid image built under main_id
    img = dm.image_name(int(main_id), app_name)
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
        await _report_replica_substatus(client, state, int(main_id), replica_id, "building_image")
    app_dir = await _download_and_extract_source(client, state, int(main_id), app_name, headers)
    await _build_image_local(int(main_id), app_name, app_dir, payload)
    return int(main_id)


async def cmd_start_replica(client, state, main_id, payload, headers):
    # Ensure the app source exists locally on this node. The replica container
    # itself still uses the main app id for naming, logs, and tunnel identity.
    app_name = payload.get("app_name") or ""
    replica_id = payload["replica_id"]

    # If the container is already running (node briefly disconnected), just
    # reconnect the tunnel instead of doing a full stop/start cycle.
    import docker_manager as dm
    if dm.is_replica_container_running(int(main_id), replica_id):
        _agent_log(f"[agent] replica={replica_id} container already running, reconnecting tunnel only")
        local_port = payload.get("external_port", 8000)
        _start_tunnel_task(state, replica_id, local_port)
        return {"container_id": None, "replica_id": replica_id, "reused": True}

    local_id = await _ensure_replica_app_deployed(client, state, main_id, payload, headers, replica_id)

    await _report_replica_substatus(client, state, int(main_id), replica_id, "creating_container")

    body = {
        "replica_id": replica_id,
        "local_app_id": local_id,
        "app_name":   app_name,
        "internal_port": payload.get("internal_port", 8000),
        "external_port": payload["external_port"],
        "env_vars": payload.get("env_vars") or {},
        "docker_options": payload.get("docker_options"),
    }
    resp = await client.post(
        f"{_LOCAL_API_BASE}/api/apps/{main_id}/replicas/run-remote",
        json=body, headers=headers, timeout=120,
    )
    resp.raise_for_status()
    result = resp.json()

    await _report_replica_substatus(client, state, int(main_id), replica_id, "waiting")

    # Start the reverse tunnel so the main node can reach the replica without
    # opening any inbound firewall ports on this node.
    local_port  = payload["external_port"]  # port the container listens on locally
    _start_tunnel_task(state, replica_id, local_port)

    return result


async def cmd_stop_replica(client, state, main_id, payload, headers):
    replica_id = payload["replica_id"]

    # Tear down the tunnel first so the main node drops the backend before the
    # container stops (prevents nginx from briefly routing to a dead port).
    await _stop_tunnel_task(replica_id)

    # Use main_id directly — no local DB lookup needed for stop.
    resp = await client.delete(
        f"{_LOCAL_API_BASE}/api/apps/{main_id}/replicas/{replica_id}/stop-remote",
        headers=headers, timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


async def cmd_refresh_source(client, state, main_id, payload, _headers):
    """Download fresh source from primary and rebuild the Docker image for this architecture.
    Called automatically after a git pull on the primary node. Always re-downloads and rebuilds."""
    app_name = payload.get("app_name") or ""
    if not app_name:
        raise RuntimeError("Missing app_name in refresh_source payload")

    app_dir = await _download_and_extract_source(client, state, int(main_id), app_name, headers={"X-Node-Token": state.auth_token})
    await _build_image_local(int(main_id), app_name, app_dir, payload)
    _agent_log(f"[refresh_source] app='{app_name}' rebuilt from updated source")
    return {"ok": True, "app_dir": app_dir}


# ─── Command Dispatcher ───────────────────────────────────────────────────────

COMMAND_HANDLERS: Dict[str, Callable] = {
    "get_logs_tail": cmd_get_logs_tail,
    "get_replica_logs": cmd_get_replica_logs,
    "get_stats": cmd_get_stats,
    "get_replica_stats": cmd_get_replica_stats,
    "get_agent_logs": cmd_get_agent_logs,
    "delete_app": cmd_delete_app,
    "start_replica": cmd_start_replica,
    "stop_replica": cmd_stop_replica,
    "refresh_source": cmd_refresh_source,
}

async def _execute_command(
    client: httpx.AsyncClient,
    state: AgentState,
    command: dict[str, Any],
    ws_main=None,
    ws_send_fn=None,
) -> tuple[str, Optional[dict[str, Any]], Optional[str]]:
    command_type = command.get("command_type") or "unknown"
    payload = command.get("payload") or {}
    main_id = str(command.get("app_id") or payload.get("app_id") or "")
    
    agent_token = _load_agent_token()
    headers = {"X-Agent-Token": agent_token, "Content-Type": "application/json"}

    try:
        # Handle streaming separately as they need ws_main
        if command_type in ("stream_logs", "stream_stats", "node_stats_stream"):
            if not ws_main: return "failed", None, "Streaming requires WebSocket"

            if command_type == "node_stats_stream":
                local_path = "/ws/system/stats"
            else:
                # main_id is used directly — no local DB lookup needed
                suffix = "logs" if command_type == "stream_logs" else "stats"
                local_path = f"/ws/apps/{main_id}/{suffix}"

            stream_id = payload.get("stream_id") or secrets.token_hex(8)
            _agent_log(f"[agent] starting {command_type} relay: local={local_path} stream_id={stream_id}")
            # Use ws_send_fn (lock-protected) when available, fall back to raw ws_main.send
            send_fn = ws_send_fn if ws_send_fn is not None else ws_main.send
            task = asyncio.create_task(_stream_relay(send_fn, stream_id, local_path))
            _active_streams[stream_id] = task
            return "streaming", {"stream_id": stream_id}, None

        # Standard commands
        handler = COMMAND_HANDLERS.get(command_type)
        if not handler:
            _agent_log(f"[agent] Unsupported command: {command_type}")
            return "failed", None, f"Unsupported command type: {command_type!r}"
        
        result = await handler(client, state, main_id, payload, headers)
        return "done", result, None

    except Exception as e:
        _agent_log(f"[agent] Command {command_type} failed: {e}")
        return "failed", None, str(e)

# ─── WebSocket / Loops ────────────────────────────────────────────────────────

_active_streams: dict[str, asyncio.Task] = {}
_pending_pings: dict[str, asyncio.Future] = {}

async def _stream_relay(send_fn, stream_id: str, local_ws_path: str) -> None:
    local_url = _local_ws_url(local_ws_path)
    agent_token = _load_agent_token()
    extra_headers = {"X-Agent-Token": agent_token} if agent_token else {}
    frames = 0
    try:
        _agent_log(f"[stream] {stream_id} connecting to {local_url}")
        async with websockets.connect(local_url, max_size=2**22, additional_headers=extra_headers) as local_ws:
            _agent_log(f"[stream] {stream_id} connected, relaying to main WS")
            async for raw in local_ws:
                frames += 1
                await send_fn(json.dumps({
                    "type": "stream_data",
                    "stream_id": stream_id,
                    "data": raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace"),
                }))
                if frames == 1:
                    _agent_log(f"[stream] {stream_id} first frame sent")
    except Exception as e:
        _agent_log(f"[stream] {stream_id} error after {frames} frames: {type(e).__name__}: {e}")
    finally:
        _active_streams.pop(stream_id, None)
        _agent_log(f"[stream] {stream_id} relay ended ({frames} frames total)")

async def _run_websocket_loop(client: httpx.AsyncClient, state: AgentState):
    ws_url = _ws_url(state.main_url)
    attempt = 0
    while True:
        _agent_log(f"[agent] Connecting websocket to {ws_url} (attempt {attempt})")
        try:
            async with websockets.connect(ws_url, max_size=2**22, ping_interval=20, ping_timeout=20) as ws:
                attempt = 0
                _agent_log(f"[agent] WebSocket connected to {ws_url}")
                # Serialise all ws.send() calls — concurrent sends corrupt the connection
                _ws_lock = asyncio.Lock()

                async def _ws_send(payload: str):
                    async with _ws_lock:
                        await ws.send(payload)

                await _ws_send(json.dumps({"type": "auth", "token": state.auth_token}))

                async def _heartbeat_task():
                    await _cleanup_orphaned_replica_containers(client, state)
                    while True:
                        await asyncio.sleep(state.heartbeat_interval)
                        await _ws_send(json.dumps({
                            "type": "heartbeat",
                            "node_metrics": _collect_node_metrics(),
                            "metadata_json": _collect_system_info(),
                            "capabilities": _build_capabilities()
                        }))
                        _heartbeat_task._cleanup_tick = getattr(_heartbeat_task, "_cleanup_tick", 0) + 1
                        if _heartbeat_task._cleanup_tick % 5 == 0:
                            await _cleanup_orphaned_replica_containers(client, state)

                hb_task = asyncio.create_task(_heartbeat_task())
                try:
                    async for message in ws:
                        data = json.loads(message)
                        mtype = data.get("type")

                        if mtype == "command":
                            cmd = data["command"]
                            status, res, err = await _execute_command(client, state, cmd, ws_main=ws, ws_send_fn=_ws_send)
                            # Streaming commands use id=-1 and have no DB record — never ACK them
                            if status != "streaming" and cmd.get("id", -1) != -1:
                                await _ws_send(json.dumps({
                                    "type": "command_result",
                                    "command_id": cmd["id"],
                                    "status": status,
                                    "result": res,
                                    "error_message": err
                                }))
                        elif mtype == "cancel_stream":
                            sid = data.get("stream_id")
                            if sid and sid in _active_streams:
                                _active_streams[sid].cancel()
                                _active_streams.pop(sid, None)
                        elif mtype == "ping":
                            await _ws_send(json.dumps({"type": "pong", "ping_id": data.get("ping_id", "")}))
                finally:
                    hb_task.cancel()
                    try:
                        await hb_task
                    except asyncio.CancelledError:
                        pass
        except Exception as e:
            backoff = min(0.5 * (2 ** attempt), 60)
            _agent_log(f"[agent] websocket loop error (attempt {attempt}): {type(e).__name__}: {e} — retrying in {backoff:.1f}s")
            attempt += 1
            await asyncio.sleep(backoff)

async def start_agent(main_url=None, invite_code=None, node_name=None, public_host=None, heartbeat_interval=15, exit_after_registration=False):
    state = _load_state()
    if not state:
        # No saved state — must register
        if not main_url or not invite_code:
            _agent_log("[agent] No saved state and no registration args provided — cannot start")
            return
        async with httpx.AsyncClient() as client:
            state = await _register(client, _normalize_url(main_url), invite_code, node_name or socket.gethostname(), public_host, heartbeat_interval)
        _save_state(state)
        _agent_log(f"[agent] Registered node '{state.node_name}' (id={state.node_id})")
        if exit_after_registration:
            _agent_log("[agent] Registration complete, exiting as requested.")
            return
    elif exit_after_registration:
        # Already registered — nothing to do
        _agent_log(f"[agent] Already registered as '{state.node_name}' (id={state.node_id}), skipping re-registration")
        return
    
    async with httpx.AsyncClient() as client:
        await _drain_stale_commands(client, state)
        await _run_websocket_loop(client, state)

async def _drain_stale_commands(client: httpx.AsyncClient, state: AgentState) -> None:
    """Fail any queued commands that were left over from a previous session."""
    headers = {"X-Node-Token": state.auth_token}
    try:
        resp = await client.get(
            f"{state.main_url}/api/nodes/agent/commands",
            headers=headers,
            timeout=10,
        )
        if resp.status_code != 200:
            return
        commands = resp.json().get("commands") or []
        for cmd in commands:
            _agent_log(f"[agent] Draining stale command {cmd['id']} ({cmd.get('command_type')})")
            await _report_result(client, state, cmd["id"], status="failed", error_message="Agent restarted — command discarded")
    except Exception as e:
        _agent_log(f"[agent] Could not drain stale commands: {e}")

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--main-url")
    parser.add_argument("--invite-code")
    parser.add_argument("--node-name")
    parser.add_argument("--public-host")
    parser.add_argument("--heartbeat-interval", type=int, default=15)
    parser.add_argument("--exit-after-registration", action="store_true")
    args = parser.parse_args()
    await start_agent(
        args.main_url, 
        args.invite_code, 
        args.node_name, 
        args.public_host, 
        args.heartbeat_interval,
        exit_after_registration=args.exit_after_registration
    )

if __name__ == "__main__":
    asyncio.run(main())
