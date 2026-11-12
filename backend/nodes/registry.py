import json
import asyncio
import time
import urllib.request
from typing import Optional
from fastapi import HTTPException
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from core import selfupdate
from core.models import Node
from nodes.commands import _utcnow
from nodes.state import _node_ws_connections

_LOCAL_INFO_REFRESH_SECONDS = 300


_LOCAL_SEEN_UPDATE_SECONDS = 30


_last_local_info_refresh_at = 0.0


def _detect_public_ip(timeout: float = 2.5) -> Optional[str]:
    """Best-effort public IPv4 detection for the primary node."""
    endpoints = (
        "https://api.ipify.org",
        "https://ipv4.icanhazip.com",
        "https://ifconfig.me/ip",
    )
    for url in endpoints:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="ignore").strip()
            # Basic IPv4 validation
            parts = raw.split(".")
            if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
                return raw
        except Exception:
            continue
    return None


async def ensure_local_node(db: AsyncSession) -> Node:
    global _last_local_info_refresh_at
    result = await db.execute(select(Node).where(Node.is_local == True))
    local = result.scalar_one_or_none()
    now = _utcnow()
    if local:
        updated = False
        if local.status != "online":
            local.status = "online"
            updated = True
        if local.offline_since is not None:
            local.offline_since = None
            updated = True
        if local.name in ("local-cloudbase", "local_cloudbase", "local-cloudbae", "local_cloudbae", "") or "local" in local.name.lower():
            local.name = "Primary Node"
            updated = True

        # last_seen is updated by the background heartbeat loop — skip here
        # to avoid lock contention on every incoming request.

        # Expensive system/public-IP discovery is throttled to avoid 2-7s request stalls.
        monotonic_now = time.monotonic()
        needs_info_refresh = (
            local.metadata_json in (None, "", "{}")
            or (monotonic_now - _last_local_info_refresh_at) >= _LOCAL_INFO_REFRESH_SECONDS
        )
        if needs_info_refresh:
            def _collect_local_info() -> tuple[dict, Optional[str]]:
                info = _local_system_info()
                public_ip = _detect_public_ip(timeout=1.0)
                return info, public_ip

            info, public_ip = await asyncio.to_thread(_collect_local_info)
            if public_ip:
                info["public_ip"] = public_ip
                local.public_host = public_ip
            local.metadata_json = json.dumps(info)
            _last_local_info_refresh_at = monotonic_now
            updated = True

        if updated:
            local.updated_at = now
            await db.commit()
            await db.refresh(local)
        return local

    local = Node(
        name="Primary Node",
        status="online",
        is_local=True,
        enabled=True,
        heartbeat_interval=15,
        capabilities=json.dumps({"local_execution": True}),
        metadata_json="{}",
        last_seen=now,
    )
    def _collect_local_info() -> tuple[dict, Optional[str]]:
        info = _local_system_info()
        public_ip = _detect_public_ip(timeout=1.0)
        return info, public_ip

    info, public_ip = await asyncio.to_thread(_collect_local_info)
    if public_ip:
        info["public_ip"] = public_ip
        local.public_host = public_ip
    local.metadata_json = json.dumps(info)
    _last_local_info_refresh_at = time.monotonic()
    db.add(local)
    await db.commit()
    await db.refresh(local)
    return local


async def get_node_or_404(node_id: int, db: AsyncSession) -> Node:
    result = await db.execute(select(Node).where(Node.id == node_id))
    node = result.scalar_one_or_none()
    if not node:
        raise HTTPException(404, "Node not found")
    return node


async def get_node_by_token_or_401(token: str, db: AsyncSession) -> Node:
    result = await db.execute(select(Node).where(and_(Node.auth_token == token, Node.enabled == True)))
    node = result.scalar_one_or_none()
    if not node:
        raise HTTPException(401, "Invalid node token")
    return node


def _local_system_info() -> dict:
    import socket as _socket
    info: dict = {}
    try:
        import psutil, platform, subprocess
        info["hostname"]  = _socket.gethostname()
        info["os"]        = platform.platform()
        info["os_short"]  = f"{platform.system()} {platform.release()}"
        info["arch"]      = platform.machine()
        
        import time as _time
        try:
            info["uptime_secs"] = round(_time.time() - psutil.boot_time())
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
        except Exception:
            pass

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

        # Try to get GPU info
        try:
            if platform.system() == "Windows":
                try:
                    gpu_out = subprocess.check_output(["wmic", "path", "win32_VideoController", "get", "name"]).decode()
                    gpus = [line.strip() for line in gpu_out.split("\n") if line.strip() and "Name" not in line]
                    if gpus: info["gpu_model"] = ", ".join(gpus)
                except Exception: pass
            else:
                try:
                    gpu_out = subprocess.check_output(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"]).decode()
                    gpus = [line.strip() for line in gpu_out.split("\n") if line.strip()]
                    if gpus: info["gpu_model"] = ", ".join(gpus)
                except Exception: pass
        except Exception:
            pass

        try:
            addrs = psutil.net_if_addrs()
            ips = []
            for iface, addr_list in addrs.items():
                if iface.startswith("lo") or "loopback" in iface.lower():
                    continue
                for a in addr_list:
                    if a.family == _socket.AF_INET:
                        ips.append(a.address)
            if ips:
                info["ip"] = ips[0]
                info["ip_all"] = ips
        except Exception:
            pass
    except Exception:
        info.setdefault("hostname", _socket.gethostname())
    return info


def _local_metrics() -> dict:
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        return {
            "cpu_percent": cpu,
            "memory_percent": mem.percent,
            "disk_percent": disk.percent,
            "memory_total_mb": round(mem.total / 1024 / 1024),
            "memory_used_mb": round(mem.used / 1024 / 1024),
        }
    except Exception:
        return {}


def _as_node_dict(node: Node) -> dict:
    return {
        "id": node.id,
        "name": node.name,
        "role": "primary" if node.is_local else "node",
        "status": node.status,
        "public_host": node.public_host,
        "is_local": bool(node.is_local),
        "enabled": bool(node.enabled),
        "heartbeat_interval": node.heartbeat_interval,
        "capabilities": json.loads(node.capabilities or "{}"),
        "metadata": json.loads(node.metadata_json or "{}"),
        "last_seen": node.last_seen.isoformat() + "Z" if node.last_seen else None,
        "offline_since": node.offline_since.isoformat() + "Z" if node.offline_since else None,
        "last_error": node.last_error,
        "agent_version": node.agent_version,
        "code_commit": selfupdate.current()["commit"] if node.is_local else node.code_commit,
        "code_version": selfupdate.current()["version"] if node.is_local else node.code_version,
        "wg_ip": node.wg_ip,
        "wg_state": node.wg_state,
        "wg_handshake_at": node.wg_handshake_at.isoformat() + "Z" if node.wg_handshake_at else None,
        "node_metrics": _local_metrics() if node.is_local else (
            {
                "cpu_percent": node.node_cpu_percent,
                "memory_percent": node.node_memory_percent,
                "disk_percent": node.node_disk_percent,
            } if node.node_cpu_percent is not None else None
        ),
        "websocket_connected": node.id in _node_ws_connections if node.id else False,
        "created_at": node.created_at.isoformat() if node.created_at else None,
        "updated_at": node.updated_at.isoformat() if node.updated_at else None,
    }
