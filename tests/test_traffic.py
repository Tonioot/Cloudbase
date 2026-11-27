"""What apps send to the public internet: the firewall counters (scripts/cloudbase-quota), the nginx logs of what
visitors got, how both are added up per app per day, and the API that reports it."""
import asyncio
import importlib.machinery
import importlib.util
import os
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "cloudbase-quota"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def helper():
    loader = importlib.machinery.SourceFileLoader("cloudbase_quota_traffic", str(SCRIPT))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("cloudbase_quota_traffic", loader))
    loader.exec_module(module)
    return module


LISTING = """Chain CLOUDBASE-TRAFFIC (1 references)
    pkts      bytes target     prot opt in     out     source               destination
      10      900 RETURN     all  --  cbwg0  *       0.0.0.0/0            0.0.0.0/0
     500   123456 RETURN     all  --  *      *       0.0.0.0/0            10.0.0.0/8
      40    52000 RETURN     all  --  *      *       172.20.0.5           0.0.0.0/0
       3      180 RETURN     all  *      *       172.20.0.6           0.0.0.0/0
       0        0 RETURN     all  --  *      *       172.20.0.7           0.0.0.0/0
"""


class _FakeIptables:
    def __init__(self, helper, monkeypatch, listing=LISTING, chain=True, jump=True):
        self.calls, self.listing, self.chain, self.jump = [], listing, chain, jump
        monkeypatch.setattr(helper, "_iptables", self.run)
        self.error = helper.HelperError

    def run(self, *args):
        self.calls.append(list(args))
        if args[0] == "-L":
            if not self.chain:
                raise self.error("no chain")
            return self.listing
        if args[0] == "-N" and self.chain:
            raise self.error("exists")
        if args[0] == "-C" and not self.jump:
            raise self.error("no rule")
        return ""


def test_the_counters_are_read_per_address_and_zeroed_in_the_same_call(helper, monkeypatch):
    fake = _FakeIptables(helper, monkeypatch)
    assert helper.read_and_zero() == {"172.20.0.5": 52000, "172.20.0.6": 180, "172.20.0.7": 0}
    assert fake.calls == [["-L", helper.TRAFFIC_CHAIN, "-n", "-v", "-x", "-Z"]]


def test_the_same_addresses_are_not_counted_again_from_scratch(helper, monkeypatch):
    fake = _FakeIptables(helper, monkeypatch)
    out = helper.traffic(["172.20.0.5", "172.20.0.6", "172.20.0.7"])
    assert out == {"bytes": {"172.20.0.5": 52000, "172.20.0.6": 180, "172.20.0.7": 0}}
    assert not any(c[0] in ("-F", "-A") for c in fake.calls)            # the rules stay as they are


def test_new_addresses_get_rules_after_everything_that_is_not_public(helper, monkeypatch):
    fake = _FakeIptables(helper, monkeypatch, chain=False, jump=False)
    helper.traffic(["172.20.0.9"])
    rules = [c[2:] for c in fake.calls if c[:2] == ["-A", helper.TRAFFIC_CHAIN]]
    assert rules[0] == ["-i", "cbwg0", "-j", "RETURN"]                    # the node network is not app traffic
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16"):
        assert ["-d", network, "-j", "RETURN"] in rules[1:-1]             # private destinations are not counted
    assert rules[-1] == ["-s", "172.20.0.9", "-j", "RETURN"]
    # mangle, where Docker's own rules cannot accept a packet before it is counted
    assert ["-A", "FORWARD", "-i", "cb+", "-j", helper.TRAFFIC_CHAIN] in fake.calls


def test_only_private_container_addresses_are_accepted(helper):
    assert helper.check_ips(["172.20.0.5", "172.20.0.5"]) == ["172.20.0.5"]
    for bad in (["8.8.8.8"], ["127.0.0.1"], ["-j ACCEPT"], "172.20.0.5", [None]):
        with pytest.raises(helper.HelperError):
            helper.check_ips(bad)


def test_bytes_go_to_the_app_the_address_was_counted_for():
    from containers.traffic import attribute
    before = {"172.20.0.5": 1, "172.20.0.6": 2}
    now = {"172.20.0.6": 3, "172.20.0.7": 4}            # .6 was given to another app's container meanwhile
    assert attribute({"172.20.0.5": 10, "172.20.0.6": 20, "172.20.0.7": 0}, before, now) == {1: 10, 2: 20}
    assert attribute({"172.20.0.7": 5}, {}, now) == {4: 5}   # after a restart: whoever has it now


def test_a_nodes_report_only_keeps_sensible_numbers():
    from nodes.reports import traffic_counts
    assert traffic_counts({"3": 100, "x": 5, "4": -1, "5": "7", "0": 9}) == {3: 100, 5: 7}
    assert traffic_counts(None) == {} and traffic_counts(["3"]) == {}


# ── what visitors got: the nginx logs ─────────────────────────────────────────
def test_the_app_config_logs_what_visitors_get_when_the_directory_exists(monkeypatch, tmp_path):
    from nginx import config
    monkeypatch.setattr(config, "TRAFFIC_LOG_DIR", str(tmp_path))
    text = config.generate_config("shop", "shop.example.com", ["10.0.0.2:3000"], app_id=7)
    assert text.startswith("log_format cloudbase_traffic_7 '$bytes_sent';")
    assert "access_log /var/log/cloudbase-traffic/app-7.log cloudbase_traffic_7" in text
    assert "access_log /var/log/nginx/access.log;" in text              # nginx's own log keeps its lines
    monkeypatch.setattr(config, "TRAFFIC_LOG_DIR", str(tmp_path / "missing"))
    assert "cloudbase_traffic" not in config.generate_config("shop", "shop.example.com", ["10.0.0.2:3000"], app_id=7)


def test_the_panel_and_static_pages_are_not_counted(monkeypatch, tmp_path):
    from nginx import config
    monkeypatch.setattr(config, "TRAFFIC_LOG_DIR", str(tmp_path))
    assert "cloudbase_traffic" not in config.generate_config("cloudbase", "panel.example.com", 8000)
    assert "cloudbase_traffic" not in config.generate_config("shop", "shop.example.com", ["10.0.0.2:3000"], app_id=7,
                                                             mode="maintenance")


def test_new_lines_are_read_once_and_a_half_written_line_waits(tmp_path):
    from nginx import traffic_logs
    log = tmp_path / "app-7.log"
    log.write_bytes(b"100\n250\n3")
    counts, offsets, reopen = traffic_logs.read_new({}, str(tmp_path))
    assert counts == {7: 350} and not reopen
    with open(log, "ab") as f:
        f.write(b"00\n50\n")
    counts, offsets, _ = traffic_logs.read_new(offsets, str(tmp_path))
    assert counts == {7: 350}                                   # 300 (finished now) and 50
    assert traffic_logs.read_new(offsets, str(tmp_path))[0] == {}


def test_a_big_log_is_moved_aside_read_to_its_end_and_removed_later(tmp_path, monkeypatch):
    from nginx import traffic_logs
    monkeypatch.setattr(traffic_logs, "ROTATE_BYTES", 10)
    log = tmp_path / "app-7.log"
    log.write_bytes(b"1000\n2000\n3000\n")
    counts, offsets, reopen = traffic_logs.read_new({}, str(tmp_path))
    assert counts == {7: 6000} and reopen                       # nginx must open a new file
    old = tmp_path / "app-7.log.old"
    assert old.exists() and not log.exists()
    with open(old, "ab") as f:                                  # nginx wrote a last line before it switched
        f.write(b"5\n")
    log.write_bytes(b"7\n")
    counts, offsets, reopen = traffic_logs.read_new(offsets, str(tmp_path))
    assert counts == {7: 12} and not reopen
    os.utime(old, (0, 0))                                       # long unused, and read to its end
    traffic_logs.read_new(offsets, str(tmp_path))
    assert not old.exists()


# ── adding up and the API ─────────────────────────────────────────────────────
@pytest.fixture
def traffic_rows(client):
    from sqlalchemy import delete
    from core.database import AsyncSessionLocal
    from core.models import AppTraffic
    yield

    async def cleanup():
        async with AsyncSessionLocal() as db:
            await db.execute(delete(AppTraffic))
            await db.commit()
    _run(cleanup())


def _app(admin, name):
    from core.database import AsyncSessionLocal
    from core.models import Application

    async def go():
        async with AsyncSessionLocal() as db:
            app = Application(name=name, repo_url="https://example.com/x.git", start_command="x", port=3000)
            db.add(app)
            await db.commit()
            return app.id
    return _run(go())


def test_counts_are_added_per_app_per_day_and_reported(admin, traffic_rows):
    from apps import traffic
    app_id = _app(admin, "traffic-shop")
    _run(traffic.add("internet", {app_id: 1000}))
    _run(traffic.add("internet", {app_id: 500, 999999: 0}))
    _run(traffic.add("visitors", {app_id: 2500}))

    r = admin.get(f"/api/apps/{app_id}/traffic?days=3")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["internet_bytes"], body["visitor_bytes"], body["total_bytes"]) == (1500, 2500, 4000)
    assert [d["day"] for d in body["days"]] == [(traffic.today() - timedelta(days=n)).isoformat() for n in (2, 1, 0)]
    assert body["days"][-1]["total_bytes"] == 4000 and body["days"][0]["total_bytes"] == 0

    everything = admin.get("/api/traffic").json()
    row = next(a for a in everything["apps"] if a["app_id"] == app_id)
    assert row == {"app_id": app_id, "app_name": "traffic-shop", "internet_bytes": 1500, "visitor_bytes": 2500,
                   "total_bytes": 4000}


def test_what_a_deleted_app_used_is_still_reported(admin, traffic_rows):
    from apps import traffic
    _run(traffic.add("visitors", {424242: 77}))
    row = next(a for a in admin.get("/api/traffic").json()["apps"] if a["app_id"] == 424242)
    assert row["app_name"] is None and row["total_bytes"] == 77


def test_a_period_must_make_sense(admin):
    today = date.today()
    assert admin.get(f"/api/traffic?start={today}&end={today - timedelta(days=1)}").status_code == 400
    assert admin.get(f"/api/traffic?start={today - timedelta(days=500)}&end={today}").status_code == 400


# ── a container is counted from its start; databases through their public port ─
def test_a_started_app_container_is_counted_at_once(monkeypatch):
    from containers import traffic
    listing = {"now": {}}
    reads = []
    monkeypatch.setattr(traffic, "container_addresses", lambda: dict(listing["now"]))
    monkeypatch.setattr(traffic.quota, "traffic", lambda ips: reads.append(ips) or listing.pop("counts", {}))
    monkeypatch.setattr(traffic, "_counted", {})
    monkeypatch.setattr(traffic, "_pending", {})
    assert not traffic.started("something-else") and not reads
    listing["now"] = {"172.20.0.9": 4}
    assert traffic.started("cloudbase-cron-4-17") and reads == [["172.20.0.9"]]
    listing["now"], listing["counts"] = {}, {"172.20.0.9": 800}          # it is gone before the next round
    assert traffic.collect() == {4: 800}
    assert traffic.collect() == {}


def test_what_a_public_database_sends_to_the_internet_is_counted():
    from dbs import proxy

    class Writer:
        def __init__(self, peer):
            self.peer = peer

        def get_extra_info(self, _name):
            return (self.peer, 5)

        def write(self, _data):
            pass

        async def drain(self):
            pass
    proxy.take_sent()
    _run(proxy.VisitorStream(3, None, Writer("93.184.216.34")).send(b"x" * 100))
    _run(proxy.VisitorStream(3, None, Writer("::ffff:93.184.216.34")).send(b"x" * 20))
    _run(proxy.VisitorStream(4, None, Writer("192.168.1.10")).send(b"x" * 100))      # the local network is free
    assert proxy.take_sent() == {3: 120} and proxy.take_sent() == {}


def test_database_traffic_is_added_up_and_reported(admin):
    from apps import traffic
    from core.database import AsyncSessionLocal

    async def add():
        async with AsyncSessionLocal() as db:
            await traffic.add_databases_in(db, {515151: 40})
            await db.commit()
            await traffic.add_databases_in(db, {515151: 2})
            await db.commit()
    _run(add())
    body = admin.get("/api/traffic").json()
    row = next(d for d in body["databases"] if d["database_id"] == 515151)
    assert row == {"database_id": 515151, "database_name": None, "internet_bytes": 42, "total_bytes": 42}


def test_builds_are_limited_when_docker_can(monkeypatch):
    from containers import buildkit
    assert buildkit.resource_limit_args(0.5, 512) == ["--resource", "memory=512m", "--resource", "memory-swap=1024m",
                                                      "--resource", "cpu-period=100000", "--resource", "cpu-quota=50000"]
    assert buildkit.resource_limit_args(None, None) == []
