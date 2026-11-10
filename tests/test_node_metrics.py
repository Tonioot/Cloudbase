"""Metrics of a server: what is collected, kept and charted; and how the node page shows them."""
import asyncio
import re
from datetime import datetime, timedelta
from pathlib import Path


from core import sysmetrics
from nodes import metrics

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
T0 = datetime(2026, 1, 1, 12, 0, 0)


def _ms(dt):
    from core.history import _ts_ms
    return _ts_ms(dt)


def _row(seconds, **values):
    from core.models import NodeMetricsHistory
    return NodeMetricsHistory(node_id=1, timestamp=T0 + timedelta(seconds=seconds), **values)


# ── collecting ────────────────────────────────────────────────────────────────
def test_a_server_reports_usage_traffic_disk_io_and_load():
    readings = sysmetrics.collect()
    assert {"cpu_percent", "memory_percent", "disk_percent", "net_rx_bytes", "net_tx_bytes"} <= set(readings)
    assert all(isinstance(v, (int, float)) for v in readings.values())


def test_the_agent_sends_the_same_readings():
    from agent.registration import _collect_node_metrics
    assert set(_collect_node_metrics()) >= {"cpu_percent", "net_rx_bytes", "net_tx_bytes"}


# ── charts ────────────────────────────────────────────────────────────────────
def test_counters_become_rates_per_second():
    rows = [_row(0, cpu_percent=10, net_rx_bytes=1000, net_tx_bytes=0, disk_read_bytes=0, disk_write_bytes=0, load_1=0.5),
            _row(30, cpu_percent=20, net_rx_bytes=31000, net_tx_bytes=3000, disk_read_bytes=60000, disk_write_bytes=0, load_1=0.7)]
    first, second = metrics.chart_samples(rows, _ms)
    assert first["net_in"] is None and first["cpu"] == 10               # the first sample has nothing to compare with
    assert second["net_in"] == 1000 and second["net_out"] == 100 and second["disk_read"] == 2000 and second["disk_write"] == 0
    assert second["cpu"] == 20 and second["load"] == 0.7


def test_a_counter_that_went_down_gives_no_rate_for_that_step():
    """The server restarted: the counters began again from zero."""
    rows = [_row(0, net_rx_bytes=9_000_000), _row(30, net_rx_bytes=500), _row(60, net_rx_bytes=30_500)]
    samples = metrics.chart_samples(rows, _ms)
    assert samples[1]["net_in"] is None and samples[2]["net_in"] == 1000


def test_missing_readings_stay_empty():
    samples = metrics.chart_samples([_row(0, cpu_percent=5), _row(30, cpu_percent=6)], _ms)
    assert samples[1]["net_in"] is None and samples[1]["disk_write"] is None and samples[1]["load"] is None


# ── keeping ───────────────────────────────────────────────────────────────────
def test_a_heartbeat_keeps_the_latest_readings_on_the_node():
    from core.models import Node
    node = Node(name="x", auth_token="x")
    metrics.store_heartbeat(node, {"cpu_percent": 12.5, "memory_percent": 40, "disk_percent": 20, "net_rx_bytes": 5, "load_1": 0.3})
    assert (node.node_cpu_percent, node.node_net_rx, node.node_load_1) == (12.5, 5, 0.3)
    metrics.store_heartbeat(node, {"cpu_percent": 13, "memory_percent": 41, "disk_percent": 20})      # an old agent: no new fields
    assert node.node_net_rx == 5 and node.node_cpu_percent == 13                                      # what it had stays


def test_a_sample_in_the_history_carries_the_new_readings():
    row = metrics.history_row(3, T0, {"cpu_percent": 1, "memory_percent": 2, "disk_percent": 3, "net_rx_bytes": 4, "disk_write_bytes": 5, "load_1": 6})
    assert (row.net_rx_bytes, row.disk_write_bytes, row.load_1, row.disk_percent) == (4, 5, 6, 3)


# ── the endpoint ──────────────────────────────────────────────────────────────
def test_the_history_endpoint_serves_rates_and_usage(admin):
    from core.database import AsyncSessionLocal
    from core.models import Node, NodeMetricsHistory
    from sqlalchemy import delete

    async def seed():
        async with AsyncSessionLocal() as db:
            node = Node(name="metrics-node", status="online", auth_token="token-metrics", is_local=False, enabled=True)
            db.add(node)
            await db.flush()
            now = datetime.utcnow()
            for i in range(3):
                db.add(NodeMetricsHistory(node_id=node.id, timestamp=now - timedelta(seconds=60 - i * 30), cpu_percent=10 + i,
                                          memory_percent=50, disk_percent=30, net_rx_bytes=1000 + i * 30000, net_tx_bytes=0,
                                          disk_read_bytes=0, disk_write_bytes=i * 6000, load_1=0.4))
            await db.commit()
            return node.id
    node_id = asyncio.run(seed())
    try:
        body = admin.get(f"/api/nodes/{node_id}/metrics/history?hours=1").json()
        last = body["samples"][-1]
        assert set(last) >= {"ts", "cpu", "mem", "disk", "net_in", "net_out", "disk_read", "disk_write", "load"}
        assert last["net_in"] == 1000 and last["disk_write"] == 200 and last["cpu"] == 12 and last["load"] == 0.4
    finally:
        async def clean():
            async with AsyncSessionLocal() as db:
                await db.execute(delete(NodeMetricsHistory).where(NodeMetricsHistory.node_id == node_id))
                await db.execute(delete(Node).where(Node.id == node_id))
                await db.commit()
        asyncio.run(clean())


def test_an_older_database_gets_the_new_columns():
    from core.database import engine
    from sqlalchemy import text

    async def columns(table):
        async with engine.connect() as conn:
            return {row[1] for row in (await conn.execute(text(f"PRAGMA table_info({table})"))).fetchall()}
    assert {"node_net_rx", "node_net_tx", "node_disk_read", "node_disk_write", "node_load_1"} <= asyncio.run(columns("nodes"))
    assert {"net_rx_bytes", "net_tx_bytes", "disk_read_bytes", "disk_write_bytes", "load_1"} <= asyncio.run(columns("node_metrics_history"))


# ── the page ──────────────────────────────────────────────────────────────────
def test_the_page_has_as_many_charts_as_an_app_and_calls_them_metrics():
    html = (FRONTEND / "node.html").read_text(encoding="utf-8")
    script = (FRONTEND / "js" / "node_detail.js").read_text(encoding="utf-8")
    assert "<h2>Metrics</h2>" in html and "<h2>Resources</h2>" not in html
    for title in ("CPU", "Memory", "Disk usage", "Network", "Disk reads and writes", "Load average"):
        assert f"title: '{title}'" in script
    assert script.count("{ key:") >= 8                             # cpu, memory, disk, in/out, read/write and load


def test_the_key_numbers_at_the_top_are_those_of_the_overview_without_a_box():
    html = (FRONTEND / "node.html").read_text(encoding="utf-8")
    for tile in ("tile-cpu", "tile-mem", "tile-disk"):
        block = html[html.index(f'id="{tile}"'):]
        assert 'class="kpi-spark' in block[:600], tile
    utils = (FRONTEND / "js" / "utils.js").read_text(encoding="utf-8")
    dashboard = (FRONTEND / "js" / "dashboard.js").read_text(encoding="utf-8")
    assert "export function sparkPoints" in utils and "sparkPoints" in dashboard and "function sparkPoints" not in dashboard


def test_long_lists_have_a_maximum_height_and_scroll_inside():
    """Shared styling (components.css): the node page and the notification settings both use it."""
    css = (FRONTEND / "css" / "components.css").read_text(encoding="utf-8")
    assert re.search(r"\.scroll-list\s*\{[^}]*max-height[^}]*overflow-y: auto", css)
    assert 'class="nd-run-wrap scroll-list"' in (FRONTEND / "node.html").read_text(encoding="utf-8")
    assert 'id="nc-log" class="scroll-list"' in (FRONTEND / "js" / "settings.js").read_text(encoding="utf-8")   # the delivery log


def test_the_agent_log_and_the_commands_share_one_frame_and_look():
    html = (FRONTEND / "node.html").read_text(encoding="utf-8")
    assert re.search(r'id="tab-log" class="node-log-frame"', html) and re.search(r'id="tab-commands" class="node-log-frame"', html)
    assert 'class="node-log-view cmd-list"' in html


def test_the_key_numbers_are_shared_styling_and_have_no_border():
    components = (FRONTEND / "css" / "components.css").read_text(encoding="utf-8")
    overview = (FRONTEND / "css" / "overview.css").read_text(encoding="utf-8")
    html = (FRONTEND / "node.html").read_text(encoding="utf-8")
    assert ".kpis {" in components and ".kpis {" not in overview               # one definition, used by both pages
    kpi = components[components.index(".kpis {"):components.index(".kpi-sub")]
    assert "border" not in kpi
    assert 'class="kpis"' in html and "nd-tile" not in html



def test_every_metrics_chart_can_be_opened_large_like_an_apps():
    script = (FRONTEND / "js" / "node_detail.js").read_text(encoding="utf-8")
    assert 'data-chart="${c.id}"' in script and "Open Large" in script and "function openLargeChart" in script
    assert "dialog-backdrop" in script and "dialog-modern" in script            # the same dialog as the app's metrics
    assert "large: true" in script and "timeLabel" in script                    # with time along the bottom
    assert "Escape" in script                                                   # and Escape closes it


def test_the_status_dot_has_a_shape_of_its_own():
    """A span in a line of text has no width or height unless it is a block: only its glow remained, as a green dash."""
    node_css = (FRONTEND / "css" / "node.css").read_text(encoding="utf-8")
    dot = node_css[node_css.index(".nd-link-dot {"):node_css.index("}", node_css.index(".nd-link-dot {"))]
    assert "display: inline-block" in dot and "width: 8px" in dot and "height: 8px" in dot
    components = (FRONTEND / "css" / "components.css").read_text(encoding="utf-8")
    assert ".kpi-value--text strong { display: inline-flex" in components
