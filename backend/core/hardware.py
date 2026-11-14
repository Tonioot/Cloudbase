"""What the machine itself says about its health: model, temperature, power supply, swap, battery, disks, network.

Sent with every heartbeat next to the rest of a node's system info (see agent/registration.py) and shown on the
node's page. Every reading is optional: a server reports what it has (a Raspberry Pi has a power supply to report on,
a virtual machine has not), and a reading that fails is left out. Nothing here needs root.
"""
import glob
import os
import shutil
import subprocess

import psutil

# Bits of the Raspberry Pi firmware's "throttled" value: now (low bits) and since boot (bits 16 and up)
_THROTTLE_NOW = {0: "undervoltage", 1: "frequency capped", 2: "throttled", 3: "temperature limit"}
_THROTTLE_SINCE_BOOT = {16: "undervoltage", 17: "frequency capped", 18: "throttled", 19: "temperature limit"}
_THROTTLE_SYSFS = "/sys/devices/platform/soc/soc:firmware/get_throttled"
SKIP_FILESYSTEMS = ("squashfs", "tmpfs", "devtmpfs", "overlay", "proc", "sysfs", "cgroup", "cgroup2", "autofs", "efivarfs", "ramfs")


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip().strip("\x00")
    except OSError:
        return ""


def model() -> str:
    """The hardware's own name: a Raspberry Pi's board, or the vendor and product of a PC or virtual machine."""
    name = _read("/proc/device-tree/model")
    if name:
        return name
    product = _read("/sys/class/dmi/id/product_name")
    vendor = _read("/sys/class/dmi/id/sys_vendor")
    return " ".join(p for p in (vendor, product) if p and p.lower() not in ("to be filled by o.e.m.", "default string"))


def temperature() -> float | None:
    """The CPU temperature in °C: the hottest of the processor's sensors, or the SoC's thermal zone."""
    try:
        sensors = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        sensors = {}
    for name in ("coretemp", "k10temp", "cpu_thermal", "cpu-thermal", "zenpower", "acpitz"):
        readings = [t.current for t in sensors.get(name, []) if t.current]
        if readings:
            return round(max(readings), 1)
    raw = _read("/sys/class/thermal/thermal_zone0/temp")
    if raw.lstrip("-").isdigit():
        value = int(raw) / 1000
        return round(value, 1) if -40 < value < 150 else None
    return None


def fans() -> list[dict]:
    try:
        return [{"name": f.label or name, "rpm": f.current} for name, fs in psutil.sensors_fans().items() for f in fs]
    except (AttributeError, OSError):
        return []


def throttled_value() -> int | None:
    """The Raspberry Pi firmware's throttle flags, from vcgencmd or (without it) from sysfs. None on other hardware."""
    if shutil.which("vcgencmd"):
        try:
            out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=3).stdout
            if "=" in out:
                return int(out.split("=", 1)[1].strip(), 16)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    raw = _read(_THROTTLE_SYSFS)
    try:
        return int(raw, 0) if raw else None
    except ValueError:
        return None


def power() -> dict | None:
    """The state of the power supply as far as the machine can tell.

    A Raspberry Pi reports undervoltage, which is a weak or failing power supply and the usual reason it crashes or
    slows down: {"now": [...], "since_boot": [...]}. A laptop reports its battery."""
    out: dict = {}
    value = throttled_value()
    if value is not None:
        out["now"] = [label for bit, label in _THROTTLE_NOW.items() if value >> bit & 1]
        out["since_boot"] = [label for bit, label in _THROTTLE_SINCE_BOOT.items() if value >> bit & 1]
    try:
        battery = psutil.sensors_battery()
    except (AttributeError, OSError):
        battery = None
    if battery is not None:
        out["battery_percent"] = round(battery.percent)
        out["on_ac"] = bool(battery.power_plugged)
    return out or None


def disks() -> list[dict]:
    """The real file systems (not the virtual ones) with their usage."""
    out, seen = [], set()
    try:
        partitions = psutil.disk_partitions(all=False)
    except OSError:
        return out
    for p in partitions:
        if p.fstype in SKIP_FILESYSTEMS or p.device in seen or p.mountpoint.startswith(("/boot/efi", "/snap", "/var/lib/docker")):
            continue
        seen.add(p.device)
        try:
            usage = psutil.disk_usage(p.mountpoint)
        except OSError:
            continue
        out.append({"mount": p.mountpoint, "device": p.device, "fstype": p.fstype, "total_gb": round(usage.total / 1024 ** 3, 1),
                    "percent": usage.percent})
    return out[:8]


def interfaces() -> list[dict]:
    """The network interfaces that are up (not loopback, not the virtual ones Docker makes for each container)."""
    out = []
    try:
        stats = psutil.net_if_stats()
    except OSError:
        return out
    for name, st in stats.items():
        if not st.isup or name.startswith(("lo", "veth", "br-", "docker")):
            continue
        out.append({"name": name, "speed_mbps": st.speed or None})
    return out[:8]


def collect() -> dict:
    """Everything above that this machine has, in one dict. Never raises."""
    out: dict = {}
    for key, fn in (("model", model), ("temperature_c", temperature), ("fans", fans), ("power", power),
                    ("disks", disks), ("interfaces", interfaces)):
        try:
            value = fn()
        except Exception:
            continue
        if value not in (None, "", [], {}):
            out[key] = value
    try:
        swap = psutil.swap_memory()
        if swap.total:
            out["swap_percent"] = swap.percent
            out["swap_total_mb"] = round(swap.total / 1024 ** 2)
    except Exception:
        pass
    try:
        out["processes"] = len(psutil.pids())
    except Exception:
        pass
    try:
        out["kernel"] = os.uname().release
    except AttributeError:
        pass                                              # Windows
    if os.path.exists("/var/run/reboot-required"):
        out["reboot_required"] = True
    try:
        freq = psutil.cpu_freq()
        if freq and freq.current:
            out["cpu_mhz"] = round(freq.current)
    except Exception:
        pass
    return out
