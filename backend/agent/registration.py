import time
import platform
import subprocess
from typing import Any, Optional
import httpx
import psutil
from agent.state import AgentState

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


# 2.x: nodes run what the primary lists (desired state) instead of obeying start/stop commands
AGENT_VERSION = "2.0.0"


def _build_capabilities() -> dict:
    return {
        "agent_version": AGENT_VERSION,
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
