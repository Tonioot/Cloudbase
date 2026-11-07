"""What is measured on a server, how it is kept, and how the node page's charts are made from it.

Besides CPU, memory and disk usage a server reports its network traffic, its disk reads and writes (both as
counters that only ever go up, since boot) and its load. Counters are stored as they come; the charts show
the rate between two samples.
"""
from core.models import Node, NodeMetricsHistory

# counter / reading in a report -> column on the node -> column in the history
FIELDS = {
    "net_rx_bytes":    ("node_net_rx",     "net_rx_bytes"),
    "net_tx_bytes":    ("node_net_tx",     "net_tx_bytes"),
    "disk_read_bytes": ("node_disk_read",  "disk_read_bytes"),
    "disk_write_bytes": ("node_disk_write", "disk_write_bytes"),
    "load_1":          ("node_load_1",     "load_1"),
}

# the counters, and the name of the rate made from each
RATES = {"net_rx_bytes": "net_in", "net_tx_bytes": "net_out", "disk_read_bytes": "disk_read", "disk_write_bytes": "disk_write"}
CHART_KEYS = ("cpu", "mem", "disk", "net_in", "net_out", "disk_read", "disk_write", "load")


def store_heartbeat(node: Node, metrics: dict) -> None:
    """Keep the latest readings of a node on the node."""
    node.node_cpu_percent = metrics.get("cpu_percent")
    node.node_memory_percent = metrics.get("memory_percent")
    node.node_disk_percent = metrics.get("disk_percent")
    for key, (column, _history) in FIELDS.items():
        if metrics.get(key) is not None:
            setattr(node, column, metrics[key])


def readings_of(node: Node) -> dict:
    """The latest readings stored on a (remote) node, in the shape of collect()."""
    out = {key: getattr(node, column) for key, (column, _h) in FIELDS.items()}
    out.update(cpu_percent=node.node_cpu_percent, memory_percent=node.node_memory_percent, disk_percent=node.node_disk_percent)
    return out


def history_row(node_id: int, when, readings: dict) -> NodeMetricsHistory:
    row = NodeMetricsHistory(node_id=node_id, timestamp=when, cpu_percent=readings.get("cpu_percent"),
                             memory_percent=readings.get("memory_percent"), disk_percent=readings.get("disk_percent"))
    for key, (_column, history_column) in FIELDS.items():
        setattr(row, history_column, readings.get(key))
    return row


def chart_samples(rows: list[NodeMetricsHistory], to_ms) -> list[dict]:
    """Samples for the charts, oldest first: usage as it was, counters turned into rates (per second).

    A counter that went down (the server restarted) gives no rate for that step."""
    samples, previous = [], None
    for r in rows:
        ts = to_ms(r.timestamp)
        sample = {"ts": ts, "cpu": r.cpu_percent, "mem": r.memory_percent, "disk": r.disk_percent, "load": r.load_1,
                  **{name: None for name in RATES.values()}}
        if previous is not None:
            seconds = (ts - previous["ts"]) / 1000
            for key, name in RATES.items():
                now, before = getattr(r, FIELDS[key][1]), previous["row"].get(key)
                if seconds > 0 and now is not None and before is not None and now >= before:
                    sample[name] = (now - before) / seconds
        previous = {"ts": ts, "row": {key: getattr(r, FIELDS[key][1]) for key in RATES}}
        samples.append(sample)
    return samples
