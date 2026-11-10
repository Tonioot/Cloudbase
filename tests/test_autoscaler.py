"""The autoscaler works out how many instances an app needs and goes there in one step, within the app's settings."""
import asyncio
import types

from apps import autoscale
from workers import autoscaler as scaler


def _app(**kw):
    base = dict(id=1, autoscale_min_instances=1, autoscale_max_instances=10, autoscale_cpu_target=40.0,
                autoscale_memory_target_mb=None, autoscale_up_seconds=60, autoscale_down_minutes=5,
                autoscale_max_step_up=None, autoscale_max_step_down=None)
    return types.SimpleNamespace(**{**base, **kw})


def test_ten_idle_instances_go_back_to_the_minimum_at_once():
    """The case that took 45 minutes: 10 instances, peak 0.6% CPU, target 40%."""
    assert scaler.plan(_app(), 10, up=(0.5, 500), down=(0.6, 600)) == -9


def test_up_adds_what_is_needed_in_one_step_within_the_maximum():
    assert scaler.plan(_app(), 2, up=(48.0, 0), down=None) == 1          # 2 × 48 / 40 = 2.4 → 3
    assert scaler.plan(_app(), 2, up=(120.0, 0), down=None) == 4         # 2 × 120 / 40 = 6
    assert scaler.plan(_app(autoscale_max_instances=4), 2, up=(120.0, 0), down=None) == 2
    assert scaler.plan(_app(autoscale_max_step_up=1), 2, up=(120.0, 0), down=None) == 1
    assert scaler.plan(_app(), 2, up=(40.0, 0), down=None) == 0          # exactly on target: fine


def test_down_leaves_headroom_and_respects_the_minimum_and_the_step():
    # 4 instances, peak 20% of a 40% target: 4 × 20 / 30 = 2.67 → 3 (not 2, which would sit at the target)
    assert scaler.plan(_app(), 4, up=None, down=(20.0, 0)) == -1
    assert scaler.plan(_app(autoscale_min_instances=3), 10, up=None, down=(1.0, 0)) == -7
    assert scaler.plan(_app(autoscale_max_step_down=2), 10, up=None, down=(1.0, 0)) == -2
    assert scaler.plan(_app(), 4, up=None, down=None) == 0               # the down window is not covered yet


def test_memory_can_be_the_reason_to_scale():
    app = _app(autoscale_memory_target_mb=256)
    assert scaler.plan(app, 2, up=(5.0, 900.0), down=None) == 2          # 900 MB / 256 = 3.5 → 4
    assert scaler.plan(app, 4, up=None, down=(5.0, 700.0)) == 0          # 700 / (256 × 0.75) = 3.6 → 4: stays
    assert scaler.plan(app, 4, up=None, down=(5.0, 300.0)) == -2         # 300 / 192 = 1.6 → 2


def test_a_changed_minimum_or_maximum_is_followed():
    assert scaler.plan(_app(autoscale_min_instances=3), 1, up=None, down=None) == 2
    assert scaler.plan(_app(autoscale_max_instances=2), 5, up=None, down=None) == -3


def test_new_instances_are_spread_and_remote_ones_go_first():
    node = lambda i, load, local=False: types.SimpleNamespace(id=i, node_cpu_percent=load, is_local=local)  # noqa: E731
    nodes = [node(1, 50, True), node(2, 10), node(3, 30)]
    assert [n.id for n in scaler.pick_nodes(nodes, 4)] == [2, 3, 1, 2]
    inst = lambda i, n: types.SimpleNamespace(id=i, node_id=n)  # noqa: E731
    instances = [inst(5, 1), inst(9, 1), inst(6, 2), inst(8, 3)]
    assert [i.id for i in scaler.pick_victims(instances, 1, 3)] == [8, 6, 9]


def test_the_settings_are_kept_within_their_limits():
    assert autoscale.clean("autoscale_cpu_target", 200) == 95
    assert autoscale.clean("autoscale_down_minutes", 0) == 1
    assert autoscale.clean("autoscale_up_seconds", 5) == 30
    assert autoscale.clean("autoscale_memory_target_mb", 0) is None      # off
    assert autoscale.clean("autoscale_max_step_up", 0) is None           # no limit
    assert autoscale.clean("autoscale_max_step_down", 3.7) == 3


def test_the_settings_can_be_saved_and_read_back(admin, client):
    from core.database import AsyncSessionLocal
    from core.models import Application

    async def make():
        async with AsyncSessionLocal() as db:
            app = Application(name="scale-settings", repo_url="https://example.com/x.git", app_type="node", start_command="node .", port=3000)
            db.add(app)
            await db.commit()
            return app.id
    app_id = asyncio.run(make())
    body = {"autoscale_enabled": True, "autoscale_memory_target_mb": 512, "autoscale_up_seconds": 90,
            "autoscale_down_minutes": 0, "autoscale_max_step_up": 2, "autoscale_max_step_down": 0}
    assert admin.put(f"/api/apps/{app_id}", json=body).status_code == 200, "save"
    app = admin.get(f"/api/apps/{app_id}").json()
    assert (app["autoscale_memory_target_mb"], app["autoscale_up_seconds"], app["autoscale_down_minutes"],
            app["autoscale_max_step_up"], app["autoscale_max_step_down"]) == (512, 90, 1, 2, None)

    async def drop():
        async with AsyncSessionLocal() as db:
            await db.delete(await db.get(Application, app_id))
            await db.commit()
    asyncio.run(drop())


def test_a_new_model_column_is_added_to_an_existing_table(tmp_path):
    from sqlalchemy import Boolean, Column, Integer, MetaData, Table, create_engine, inspect, text
    from core import database
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE things (id INTEGER PRIMARY KEY)")
        conn.exec_driver_sql("INSERT INTO things (id) VALUES (1)")
    metadata = MetaData()
    Table("things", metadata, Column("id", Integer, primary_key=True), Column("size", Integer, default=5),
          Column("on", Boolean, default=False), Column("note", Integer, nullable=True))
    real = database.Base.metadata
    database.Base.metadata = metadata
    try:
        with engine.begin() as conn:
            database._add_new_columns(conn)
            database._add_new_columns(conn)                                # a second start changes nothing
    finally:
        database.Base.metadata = real
    assert {c["name"] for c in inspect(engine).get_columns("things")} == {"id", "size", "on", "note"}
    with engine.connect() as conn:
        assert tuple(conn.execute(text('SELECT size, "on", note FROM things')).one()) == (5, 0, None)



def test_an_idle_app_is_scaled_down_in_one_action(client, monkeypatch):
    """From the database to the action: ten running instances, idle for the whole window → one action removing nine."""
    from core.database import AsyncSessionLocal
    from core.models import Application, Instance
    from nodes.registry import ensure_local_node
    from apps import state as app_state
    from sqlalchemy import select
    actions = []

    async def scale_down(app_id, count, victims, reason):
        actions.append((count, [v for v, _ in victims]))
    monkeypatch.setattr(scaler, "_scale_down", scale_down)
    monkeypatch.setattr(scaler, "_last_scaled", {})
    monkeypatch.setattr(scaler, "recent_average", lambda app_id, seconds: (0.5, 400.0))

    async def peak(db, app_id, seconds):
        return 0.6, 400.0
    monkeypatch.setattr(scaler, "history_peak", peak)

    async def go():
        async with AsyncSessionLocal() as db:
            local = await ensure_local_node(db)
            app = Application(name="scale-idle", repo_url="https://example.com/x.git", app_type="node", start_command="node .",
                              port=3000, autoscale_enabled=True, autoscale_cpu_target=40.0, autoscale_max_instances=10)
            db.add(app)
            await db.flush()
            db.add_all([Instance(app_id=app.id, node_id=local.id, status="running") for _ in range(10)])
            await db.commit()
            ids = sorted(i.id for i in (await db.execute(select(Instance).where(Instance.app_id == app.id))).scalars())
            try:
                await scaler.check_app(db, app, local, 1000.0)
                await asyncio.sleep(0.05)                        # the action runs in the background
            finally:
                app_state.release_app(app.id)
                await db.delete(app)
                await db.commit()
            return ids
    ids = asyncio.run(go())
    assert actions == [(10, sorted(ids, reverse=True)[:9])]          # newest first, the oldest one stays
