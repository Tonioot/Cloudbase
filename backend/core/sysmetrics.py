"""Readings of the machine this process runs on (CPU, memory, disk, network, load). Needs nothing but psutil,
so the node agent can use it without loading any of the panel."""
import os

import psutil


def collect() -> dict:
    """The readings of this machine, in the shape of a heartbeat (the agent and the primary's own sampler share it)."""
    out: dict = {}
    try:
        out.update(cpu_percent=psutil.cpu_percent(interval=None), memory_percent=psutil.virtual_memory().percent,
                   disk_percent=psutil.disk_usage("/").percent)
    except Exception:
        pass
    try:
        net = psutil.net_io_counters()
        out.update(net_rx_bytes=net.bytes_recv, net_tx_bytes=net.bytes_sent)
    except Exception:
        pass
    try:
        io = psutil.disk_io_counters()
        if io:
            out.update(disk_read_bytes=io.read_bytes, disk_write_bytes=io.write_bytes)
    except Exception:
        pass
    try:
        out["load_1"] = round(os.getloadavg()[0], 2)
    except (AttributeError, OSError):
        pass                                              # Windows has no load average
    return out


