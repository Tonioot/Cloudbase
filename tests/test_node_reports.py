"""Nodes send the stats of their instances and the state of their databases by themselves (agent/report.py);
the primary only keeps the latest (nodes/reports.py) and uses it where it used to ask."""
import asyncio
import time

import pytest


def _run(coro):
    return asyncio.run(coro)


# ── the node's side ──────────────────────────────────────────────────────────
@pytest.fixture
def node_side(monkeypatch):
    from agent import report
    containers = [
        {"name": "cloudbase-app-1-instance-11", "app_id": 1, "instance_id": 11, "status": "running"},
        {"name": "cloudbase-app-2-instance-21", "app_id": 2, "instance_id": 21, "status": "running"},
        {"name": "cloudbase-app-2-instance-22", "app_id": 2, "instance_id": 22, "status": "exited"},
    ]
    asked, db_calls = [], []
    monkeypatch.setattr(report.containers_instances, "list_instance_containers", lambda: containers)

    def stats(name):
        asked.append(name)
        return {"cpu_percent": 5.0, "memory_mb": 10.0}
    monkeypatch.setattr(report.containers_stats_logs, "get_container_stats_by_name", stats)

    def report_all(with_stats, with_disk):
        db_calls.append((with_stats, with_disk))
        return {"7": {"state": "running", "volume": True}}
    monkeypatch.setattr(report.dbs_lifecycle, "report_all", report_all)
    monkeypatch.setattr(report, "_watched", {})
    return report, asked, db_calls


def _send_into(messages):
    async def send(message):
        messages.append(message)
    return send


def test_every_running_instance_every_ten_seconds_and_databases_every_thirty(node_side):
    report, asked, db_calls = node_side
    messages = []
    for tick in range(16):                                         # 32 s
        _run(report.report_once(tick, _send_into(messages)))
    instance_rounds = [m for m in messages if m["type"] == "instance_stats"]
    assert len(instance_rounds) == 4                               # at 0, 10, 20, 30 s
    assert set(instance_rounds[0]["stats"]) == {"11", "21"}        # running ones only
    assert all(s["timestamp"] > 0 for s in instance_rounds[0]["stats"].values())
    assert [m["databases"] for m in messages if m["type"] == "database_report"] == [{"7": {"state": "running", "volume": True}}] * 2
    assert db_calls == [(True, True), (False, False)]              # usage every other round, the data size now and then


def test_a_watched_app_is_sent_every_tick_until_nobody_looks(node_side, monkeypatch):
    report, asked, _ = node_side
    messages = []
    report.watch(2)
    _run(report.report_once(1, _send_into(messages)))              # not a full round: only the watched app
    assert messages == [{"type": "instance_stats", "stats": {"21": messages[0]["stats"]["21"]}}]
    report._watched[2] = time.monotonic() - 1                       # its WATCH_SECONDS are over
    _run(report.report_once(1, _send_into(messages)))
    assert len(messages) == 1                                       # nobody looks any more: wait for the full round


def test_a_failing_part_does_not_stop_the_other(node_side, monkeypatch):
    report, _, _ = node_side

    def broken():
        raise RuntimeError("docker is gone")
    monkeypatch.setattr(report.containers_instances, "list_instance_containers", broken)
    messages = []
    _run(report.report_once(0, _send_into(messages)))
    assert [m["type"] for m in messages] == ["database_report"]


# ── the primary's side ───────────────────────────────────────────────────────
@pytest.fixture
def world(client):
    from sqlalchemy import delete
    from core.database import AsyncSessionLocal
    from core.models import Application, Database, Instance, Node
    made = {"apps": [], "nodes": [], "dbs": []}

    class World:
        def node(self, name):
            async def go():
                async with AsyncSessionLocal() as db:
                    row = Node(name=name, status="online", auth_token=f"token-{name}", is_local=False, enabled=True, agent_version="4.0.0")
                    db.add(row)
                    await db.commit()
                    return row.id
            made["nodes"].append(_run(go()))
            return made["nodes"][-1], f"token-{name}"

        def instance(self, name, node_id):
            async def go():
                async with AsyncSessionLocal() as db:
                    app = Application(name=name, repo_url="https://example.test/x", start_command="x", port=3000)
                    db.add(app)
                    await db.flush()
                    inst = Instance(app_id=app.id, node_id=node_id, status="running", external_port=14000 + app.id)
                    db.add(inst)
                    await db.commit()
                    return app.id, inst.id
            app_id, iid = _run(go())
            made["apps"].append(app_id)
            return iid

        def database(self, name, node_id, status="running"):
            from core.env_crypto import encrypt_text
            async def go():
                async with AsyncSessionLocal() as db:
                    row = Database(name=name, engine="postgres", version="16", node_id=node_id, status=status, desired_state="running",
                                   credentials=encrypt_text("{}"))
                    db.add(row)
                    await db.commit()
                    return row.id
            made["dbs"].append(_run(go()))
            return made["dbs"][-1]

    yield World()

    async def cleanup():
        async with AsyncSessionLocal() as db:
            await db.execute(delete(Instance).where(Instance.app_id.in_(made["apps"])))
            await db.execute(delete(Application).where(Application.id.in_(made["apps"])))
            await db.execute(delete(Database).where(Database.id.in_(made["dbs"])))
            await db.execute(delete(Node).where(Node.id.in_(made["nodes"])))
            await db.commit()
    _run(cleanup())


def _connect(admin, token):
    ws = admin.websocket_connect("/api/nodes/ws/agent")
    sock = ws.__enter__()
    sock.send_json({"type": "auth", "token": token, "session": "s1"})
    assert sock.receive_json()["type"] == "auth_ok"
    return ws, sock


def _wait_for(check, seconds=5.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return False


def test_what_a_node_sends_is_kept_for_its_own_instances_only(admin, world):
    from nodes import reports
    node_id, token = world.node("rp-mine")
    other_id, _ = world.node("rp-other")
    mine, not_mine = world.instance("rp-mine-app", node_id), world.instance("rp-other-app", other_id)
    ws, sock = _connect(admin, token)
    try:
        sock.send_json({"type": "instance_stats", "stats": {str(mine): {"cpu_percent": 12.0}, str(not_mine): {"cpu_percent": 99.0}}})
        assert _wait_for(lambda: reports.instance_stats(mine) is not None)
        assert reports.instance_stats(mine)["cpu_percent"] == 12.0
        assert reports.instance_stats(not_mine) is None                 # a node cannot speak for another node's instance
    finally:
        ws.__exit__(None, None, None)


def test_a_database_report_is_kept_and_its_usage_survives_reports_without_it(admin, world):
    from nodes import reports
    node_id, token = world.node("rp-dbs")
    ws, sock = _connect(admin, token)
    try:
        sock.send_json({"type": "database_report", "databases": {"5": {"state": "running", "volume": True,
                                                                          "stats": {"cpu_percent": 3.0, "disk_mb": 40.0}}}})
        sock.send_json({"type": "database_report", "databases": {"5": {"state": "running", "volume": True}}})
        assert _wait_for(lambda: (reports.databases(node_id) or (0, {}))[1].get("5", {}).get("state") == "running")
    finally:
        ws.__exit__(None, None, None)
    assert reports.database_stats(5) == {"disk_mb": 40.0, "cpu_percent": 3.0}


def test_the_database_monitor_reads_the_report_and_ignores_one_from_before_an_operation(world, monkeypatch):
    from core.database import AsyncSessionLocal
    from core.models import Node
    from dbs import state as dbs_state, workers
    from nodes import reports
    node_id, _ = world.node("rp-monitor")
    up, gone, busy_before = world.database("rp-up", node_id), world.database("rp-gone", node_id), world.database("rp-op", node_id)
    reports.store_databases(node_id, {str(up): {"state": "running", "volume": True},
                                      str(busy_before): {"state": "stopped", "volume": True}})
    dbs_state._released_at[busy_before] = time.monotonic() + 1               # its operation ended after this report

    async def go():
        async with AsyncSessionLocal() as db:
            node = await db.get(Node, node_id)
        return await workers._fetch_states(node, [up, gone, busy_before])
    states = _run(go())
    assert states[str(up)] == {"state": "running", "volume": True}
    assert states[str(gone)] == {"state": "missing", "volume": False}          # neither container nor volume there
    assert str(busy_before) not in states                                     # the report is older than what happened since


def test_without_a_recent_report_the_monitor_does_not_guess(world, monkeypatch):
    from core.database import AsyncSessionLocal
    from core.models import Node
    from dbs import workers
    from nodes import reports
    node_id, _ = world.node("rp-silent")
    monkeypatch.setattr(reports, "_databases", {node_id: (time.monotonic() - reports.DATABASE_MAX_AGE_SECONDS - 1, {})})

    async def go():
        async with AsyncSessionLocal() as db:
            return await workers._fetch_states(await db.get(Node, node_id), [1])
    with pytest.raises(RuntimeError, match="no recent report"):
        _run(go())


# ── each machine cleans its own disk ─────────────────────────────────────────
@pytest.mark.parametrize("disk,since,expected", [
    (50.0, 3600 * 23, None),             # not a day yet
    (50.0, 86400, False),                # daily, routine
    (85.0, 3600, False),                 # 80% full: hourly
    (85.0, 1800, None),
    (95.0, 3600, True),                  # 90% full: aggressive
    (None, 86400, False),                # disk unknown: routine, daily
])
def test_when_a_machine_cleans_its_docker_disk(disk, since, expected):
    from containers.cleanup import cleanup_due
    assert cleanup_due(disk, since) == expected


def test_a_node_finds_its_databases_by_name_and_measures_the_running_ones(monkeypatch):
    from types import SimpleNamespace as N
    from dbs import lifecycle
    containers = [N(name="cloudbase-db-3", labels={"cloudbase.engine": "postgres", "cloudbase.version": "16"}),
                  N(name="cloudbase-db-4", labels={}), N(name="cloudbase-db-backup-tmp", labels={})]
    volumes = [N(name="cloudbase-db-3-data"), N(name="cloudbase-db-9-data"), N(name="other")]

    class Client:
        class containers:
            @staticmethod
            def list(all=False, filters=None):
                return containers
        class volumes:
            @staticmethod
            def list(filters=None):
                return volumes
    asked = []
    monkeypatch.setattr(lifecycle, "_client", lambda: Client)
    monkeypatch.setattr(lifecycle, "_get_container", lambda client, db_id: next(c for c in containers if c.name == f"cloudbase-db-{db_id}"))
    monkeypatch.setattr(lifecycle, "states", lambda ids: {str(i): {"state": {3: "running", 4: "stopped"}.get(i, "missing"),
                                                                  "volume": i in (3, 9)} for i in ids})
    monkeypatch.setattr(lifecycle, "stats", lambda db_id, data_dir: asked.append((db_id, data_dir)) or {"cpu_percent": 1.0})
    out = lifecycle.report_all(with_stats=True, with_disk=True)
    assert set(out) == {"3", "4", "9"}                                   # by container, by data volume, and nothing else
    assert out["3"]["stats"] == {"cpu_percent": 1.0} and "stats" not in out["4"] and "stats" not in out["9"]
    assert asked == [(3, lifecycle.db_engines.get_engine("postgres").data_dir("16"))]
