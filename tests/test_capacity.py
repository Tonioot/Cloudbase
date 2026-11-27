"""Reservations (nodes/capacity.py): memory is never promised twice, CPU a little, deploys keep room for one more instance,
what already runs is left alone, and something new only goes where it fits."""
import asyncio
import json
import itertools

import pytest

_names = itertools.count(1)
_made = {"nodes": [], "apps": [], "instances": [], "databases": []}      # removed again after every test (shared database)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def cap(client, monkeypatch):
    """Reservations on, with settings a test may change; tenant mode stays off (no defaults from it)."""
    from core import config as _cfg
    values = {"mode": "on", "reserve_cpu": 0.5, "reserve_memory_mb": 1024, "reserve_disk_gb": 10, "cpu_ratio": 2.0,
              "deploy_room": False}
    monkeypatch.setattr(_cfg, "get_capacity", lambda: dict(values))
    yield values
    _clean_up()


def _clean_up():
    from sqlalchemy import delete
    from core.database import AsyncSessionLocal
    from core.models import Application, Database, Instance, Node

    async def go():
        async with AsyncSessionLocal() as db:
            for model, key in ((Instance, "instances"), (Database, "databases"), (Application, "apps"), (Node, "nodes")):
                if _made[key]:
                    await db.execute(delete(model).where(model.id.in_(_made[key])))
                _made[key].clear()
            await db.commit()
    _run(go())


def _node(cores=4, memory_mb=4096, disk_gb=100):
    from core.database import AsyncSessionLocal
    from core.models import Node
    name = f"cap-{next(_names)}"

    async def go():
        async with AsyncSessionLocal() as db:
            # no node network address or key: these nodes are not peers of the mesh other tests look at
            node = Node(name=name, status="online", auth_token=f"t-{name}", is_local=False, enabled=True, agent_version="5.0.0",
                        metadata_json=json.dumps({"cpu_count_logical": cores, "ram_total_mb": memory_mb, "disk_total_gb": disk_gb}))
            db.add(node)
            await db.commit()
            _made["nodes"].append(node.id)
            return node.id
    return _run(go())


def _app(cpu=None, memory_mb=None, cron=None, node_id=None):
    from core.database import AsyncSessionLocal
    from core.models import Application

    async def go():
        async with AsyncSessionLocal() as db:
            app = Application(name=f"cap-app-{next(_names)}", repo_url="https://example.test/x", start_command="npm start", port=3000,
                              docker_cpu_limit=cpu, docker_memory_limit_mb=memory_mb, cron_schedule=cron, node_id=node_id)
            db.add(app)
            await db.commit()
            _made["apps"].append(app.id)
            return app.id
    return _run(go())


def _instance(app_id, node_id, desired="running"):
    from core.database import AsyncSessionLocal
    from core.models import Instance

    async def go():
        async with AsyncSessionLocal() as db:
            row = Instance(app_id=app_id, node_id=node_id, external_port=20000 + next(_names), status="running", desired=desired)
            db.add(row)
            await db.commit()
            _made["instances"].append(row.id)
            return row.id
    return _run(go())


def _database(node_id, memory_mb=1024, disk_mb=10240, running=True):
    from core.database import AsyncSessionLocal
    from core.models import Database

    async def go():
        async with AsyncSessionLocal() as db:
            from sqlalchemy import func, select
            next_id = ((await db.execute(select(func.max(Database.id)))).scalar() or 0) + 1
            row = Database(id=next_id, name=f"capdb{next(_names)}", engine="postgres", version="17", node_id=node_id,
                           status="running", desired_state="running" if running else "stopped", credentials="x",
                           cpu_limit=1.0, memory_limit_mb=memory_mb, disk_limit_mb=disk_mb)
            db.add(row)
            await db.commit()
            _made["databases"].append(row.id)
            return row.id
    return _run(go())


def _with_db(fn):
    from core.database import AsyncSessionLocal

    async def go():
        async with AsyncSessionLocal() as db:
            return await fn(db)
    return _run(go())


def _node_row(db, node_id):
    from core.models import Node
    return db.get(Node, node_id)


def test_a_node_gives_out_what_it_has_minus_the_reserve(cap):
    from nodes import capacity
    node_id = _node(cores=4, memory_mb=8192, disk_gb=100)

    async def go(db):
        return capacity.allocatable(await _node_row(db, node_id))
    got = _with_db(go)
    assert (got.cpu, got.memory_mb, got.disk_mb) == (7.0, 7168, 90 * 1024)        # (4 - 0.5) x 2, 8192 - 1024, 100 - 10 GB


def test_memory_is_never_promised_twice_cpu_a_little(cap):
    from nodes import capacity
    node_id = _node(cores=2, memory_mb=4096)                                         # gives out 3 CPU, 3072 MB
    _instance(_app(cpu=1.0, memory_mb=1024), node_id)
    _instance(_app(cpu=1.0, memory_mb=1024), node_id)

    async def fits(db, need):
        node = await _node_row(db, node_id)
        try:
            await capacity.ensure(db, node, need)
            return True
        except capacity.NoRoom as e:
            return str(e.detail)
    assert _with_db(lambda db: fits(db, capacity.Need(1.0, 1024))) is True
    no = _with_db(lambda db: fits(db, capacity.Need(0.5, 1025)))
    assert "1025 MB memory asked, 1024 MB free" in no
    assert "2 CPU asked" in _with_db(lambda db: fits(db, capacity.Need(2.0, 256)))       # 2 of 3 promised already


def test_a_database_keeps_its_disk_also_when_it_is_stopped(cap):
    from nodes import capacity
    node_id = _node(cores=4, memory_mb=8192, disk_gb=30)                             # 20 GB to give out
    _database(node_id, memory_mb=2048, disk_mb=15 * 1024, running=False)

    async def go(db):
        room = (await capacity.rooms(db, [await _node_row(db, node_id)]))[0]
        return room.promised
    promised = _with_db(go)
    assert (promised.memory_mb, promised.disk_mb) == (0, 15 * 1024)


def test_a_cron_job_is_promised_on_its_node(cap):
    from nodes import capacity
    node_id = _node()
    _app(cpu=0.5, memory_mb=768, cron="*/5 * * * *", node_id=node_id)

    async def go(db):
        return (await capacity.rooms(db, [await _node_row(db, node_id)]))[0].promised
    assert _with_db(go).memory_mb == 768


def test_deploys_keep_room_for_one_more_instance(cap):
    from nodes import capacity
    cap["deploy_room"] = True
    node_id = _node(cores=4, memory_mb=4096)                                         # 3072 MB
    _instance(_app(cpu=0.5, memory_mb=1024), node_id)                                # 2048 left, 1024 kept for a deploy

    async def try_(db, surge):
        node = await _node_row(db, node_id)
        try:
            await capacity.ensure(db, node, capacity.Need(0.5, 1536), surge=surge)
            return True
        except capacity.NoRoom:
            return False
    assert _with_db(lambda db: try_(db, False)) is False      # 1536 + the 1536 a deploy of it would need > 2048
    assert _with_db(lambda db: try_(db, True)) is True        # the new version of a deploy may use the room


def test_choose_takes_a_node_where_it_fits_and_spreads(cap):
    from nodes import capacity
    full, roomy = _node(cores=2, memory_mb=2048), _node(cores=2, memory_mb=8192)    # 1024 MB and 7168 MB to give out

    async def go(db, need, spread=None):
        nodes = [await _node_row(db, full), await _node_row(db, roomy)]
        try:
            return (await capacity.choose(db, nodes, need, spread=spread)).id
        except capacity.NoRoom as e:
            return str(e.detail)
    assert _with_db(lambda db: go(db, capacity.Need(0.5, 2048))) == roomy
    assert _with_db(lambda db: go(db, capacity.Need(0.5, 512), {roomy: 1})) == full         # fits on both: spread wins
    assert "No room for" in _with_db(lambda db: go(db, capacity.Need(0.5, 9000)))

    async def many(db):
        nodes = [await _node_row(db, full), await _node_row(db, roomy)]
        return [n.id for n in await capacity.choose_many(db, nodes, capacity.Need(0.5, 3000), 5)]
    assert _with_db(many) == [roomy, roomy]                                          # 7168 MB holds two of 3000, the small node none


def test_a_restart_is_never_refused_but_growing_is(cap):
    """What already runs is left alone, even on a node that became too full: a restart asks for no more."""
    from core.database import AsyncSessionLocal
    from nodes import capacity, desired
    node_id = _node(cores=2, memory_mb=2048)                                         # 1024 MB to give out
    app_id = _app(cpu=0.5, memory_mb=1024)
    instance_id = _instance(app_id, node_id, desired="stopped")
    payload = {"app_id": app_id, "docker_cpu_limit": 0.5, "docker_memory_limit_mb": 1024, "docker_options": {}}

    async def start(p):
        async with AsyncSessionLocal() as db:
            try:
                await desired.start(db, node_id=node_id, instance_id=instance_id, payload=p)
                await db.commit()
                return True
            except capacity.NoRoom:
                await db.rollback()
                return False
    assert _run(start(payload)) is True
    _instance(_app(cpu=0.5, memory_mb=1024), node_id)                                # now the node is over its memory
    assert _run(start(payload)) is True                                              # restart: same limits
    assert _run(start({**payload, "docker_memory_limit_mb": 2048})) is False          # growing: no room


def test_off_places_as_before(cap):
    from nodes import capacity
    cap["mode"] = "off"
    node_id = _node(cores=1, memory_mb=1024)                                         # nothing to give out

    async def go(db):
        node = await _node_row(db, node_id)
        await capacity.ensure(db, node, capacity.Need(4.0, 99999))
        return (await capacity.choose(db, [node], capacity.Need(4.0, 99999))).id
    assert _with_db(go) == node_id


def test_the_settings_can_be_read_and_changed_within_limits(admin, monkeypatch):
    from core import config as _cfg
    saved = {}
    monkeypatch.setattr(_cfg, "save_config_sections", lambda sections: saved.update(sections))
    r = admin.get("/api/system/capacity")
    assert r.status_code == 200 and {"settings", "nodes", "total", "enabled"} <= set(r.json())
    assert admin.put("/api/system/capacity", json={"cpu_ratio": 40}).status_code == 400
    assert admin.put("/api/system/capacity", json={"mode": "sometimes"}).status_code == 400
    r = admin.put("/api/system/capacity", json={"cpu_ratio": 1.5, "reserve_memory_mb": 768, "mode": "on"})
    assert r.status_code == 200 and saved["capacity"] == {"cpu_ratio": 1.5, "reserve_memory_mb": 768, "mode": "on"}


# ── the kept room is shared: deploys and cron jobs use it by turns ───────────
def test_with_the_shared_room_a_cron_job_is_not_promised_but_the_room_is_as_big(cap):
    from nodes import capacity
    cap["deploy_room"] = True
    node_id = _node(cores=4, memory_mb=3072)                                         # 2048 MB
    _app(cpu=0.5, memory_mb=768, cron="*/5 * * * *", node_id=node_id)
    _app(cpu=0.5, memory_mb=768, cron="0 * * * *", node_id=node_id)

    async def go(db):
        return (await capacity.rooms(db, [await _node_row(db, node_id)]))[0]
    room = _with_db(go)
    assert (room.promised.memory_mb, room.biggest.memory_mb) == (0, 768)             # 1280 MB left for apps beside the room

    async def another_job(db, memory):
        try:
            await capacity.ensure(db, await _node_row(db, node_id), capacity.Need(0.5, memory), job=True)
            return True
        except capacity.NoRoom:
            return False
    assert _with_db(lambda db: another_job(db, 2048)) is True                        # the room can be that big
    assert _with_db(lambda db: another_job(db, 2049)) is False


def test_runs_take_the_room_by_turns_and_move_to_a_node_that_has_it(cap, monkeypatch):
    from nodes import capacity
    cap["deploy_room"] = True
    home, other = _node(cores=4, memory_mb=2048), _node(cores=4, memory_mb=2048)    # 1024 MB each
    _instance(_app(cpu=0.5, memory_mb=512), home)                                    # 512 MB left on home: the room
    _instance(_app(cpu=0.5, memory_mb=512), other)
    need = capacity.Need(0.5, 512)

    async def claim(db, key):
        node = await capacity.claim(db, need, key, prefer=home)
        return node.id if node else None
    async def only(db):
        return [await _node_row(db, home), await _node_row(db, other)]
    monkeypatch.setattr(capacity, "runnable_nodes", only)
    try:
        got = [_with_db(lambda db, k=k: claim(db, k)) for k in ("t:1", "t:2", "t:3")]
        assert got == [home, other, None]

        async def surge(db):                        # a deploy waits while a run sits in the room
            node = await _node_row(db, home)
            return await capacity.deploy_target(db, node, need, surge=True)
        target = _with_db(surge)
        assert target is None
        capacity.release("t:1")
        assert _with_db(surge).id == home
    finally:
        for k in ("t:1", "t:2", "t:3"):
            capacity.release(k)


def test_waiting_for_room_ends(cap, monkeypatch):
    """Nothing waits forever: it gives up at once when it can never fit, and after the limit when the room stays busy."""
    from nodes import capacity
    cap["deploy_room"] = True
    node_id = _node(cores=4, memory_mb=2048)
    monkeypatch.setattr(capacity, "POLL_SECONDS", 0.01)

    async def only(db):
        return [await _node_row(db, node_id)]
    monkeypatch.setattr(capacity, "runnable_nodes", only)
    with pytest.raises(capacity.NoRoom):
        _run(capacity.wait_to_claim(capacity.Need(0.5, 4096), "t:big", wait_seconds=30))
    capacity.occupy(node_id, "t:busy", capacity.Need(0.5, 1024))
    said = []
    try:
        with pytest.raises(capacity.NoRoom):
            _run(capacity.wait_to_claim(capacity.Need(0.5, 512), "t:wait", wait_seconds=0.05, say=said.append))
        assert len(said) == 1

        async def free_soon():
            task = asyncio.create_task(capacity.wait_to_claim(capacity.Need(0.5, 512), "t:wait", wait_seconds=30))
            await asyncio.sleep(0.05)
            capacity.release("t:busy")
            return (await task).id
        assert _run(free_soon()) == node_id
    finally:
        capacity.release("t:busy")
        capacity.release("t:wait")


def test_a_deploy_goes_to_another_node_when_its_own_is_full(cap, monkeypatch):
    from nodes import capacity
    cap["deploy_room"] = True
    full, roomy = _node(cores=4, memory_mb=2048), _node(cores=4, memory_mb=8192)
    _instance(_app(cpu=0.5, memory_mb=1024), full)

    async def only(db):
        return [await _node_row(db, full), await _node_row(db, roomy)]
    monkeypatch.setattr(capacity, "runnable_nodes", only)

    async def go(db):
        return (await capacity.deploy_target(db, await _node_row(db, full), capacity.Need(0.5, 512), surge=True)).id
    assert _with_db(go) == roomy


def test_an_app_id_is_never_given_out_twice(client):
    from core.database import AsyncSessionLocal
    from core.models import Application

    async def make():
        async with AsyncSessionLocal() as db:
            apps = [Application(name=f"cap-id-{next(_names)}", repo_url="https://example.test/x", start_command="x", port=1)
                    for _ in range(2)]
            db.add_all(apps)
            await db.commit()
            ids = [a.id for a in apps]
            for a in apps:
                await db.delete(a)
            await db.commit()
            return ids
    first, second = _run(make()), _run(make())
    assert len(set(first + second)) == 4 and min(second) > max(first)



def test_a_cron_run_moves_when_its_own_node_is_busy(cap, monkeypatch):
    from core.database import AsyncSessionLocal
    from core.models import Application
    from cron import scheduler
    from nodes import capacity
    cap["deploy_room"] = True
    home, other = _node(cores=4, memory_mb=2048), _node(cores=4, memory_mb=2048)    # 1024 MB each
    app_id = _app(cpu=0.5, memory_mb=768, cron="0 * * * *", node_id=home)

    async def only(db):
        return [await _node_row(db, home), await _node_row(db, other)]
    monkeypatch.setattr(capacity, "runnable_nodes", only)
    capacity.occupy(home, "t:busy", capacity.Need(0.5, 768))
    said = []

    async def go():
        async with AsyncSessionLocal() as db:
            app, node = await db.get(Application, app_id), await _node_row(db, home)
        return (await scheduler._node_with_room(app, node, 0, "t:run", said.append)).id
    try:
        assert _run(go()) == other and "goes to" in said[-1]
        assert capacity.temporary(other).memory_mb == 768
    finally:
        capacity.release("t:busy")
        capacity.release("t:run")


def test_the_disk_for_containers_is_not_for_databases_and_a_nearly_full_one_takes_nothing_new(cap):
    from nodes import capacity
    cap["deploy_room"] = True
    node_id = _node(cores=4, memory_mb=8192, disk_gb=150)

    async def look(db, percent):
        node = await _node_row(db, node_id)
        node.metadata_json = json.dumps({"cpu_count_logical": 4, "ram_total_mb": 8192, "disk_total_gb": 150,
                                         "container_area_gb": 50, "container_disk_percent": percent})
        room = (await capacity.rooms(db, [node]))[0]
        need = capacity.Need(0.5, 512)
        return (room.allocatable.disk_mb, room.fits(need), room.fits(need, surge=True),
                room.fits(need, instance=False), room.fits(need, instance=False, new=False))
    disk, new, deploy, database, bigger_disk = _with_db(lambda db: look(db, 91))
    assert disk == 90 * 1024                                                   # 150 - 50 for containers - 10 kept
    assert "91% full" in new and "91% full" in database
    assert deploy is None and bigger_disk is None
    assert _with_db(lambda db: look(db, 50))[1] is None


def test_only_what_deleted_apps_left_is_removed(tmp_path, monkeypatch):
    from containers import cleanup, processes
    for name in ("app-1", "app-2", "app-2.tmp", "app-9", "something"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(processes, "APPS_BASE_DIR", str(tmp_path))
    removed = []

    class Image:
        def __init__(self, tag):
            self.tags = [tag]

    class Images:
        def list(self):
            return [Image(t) for t in ("cloudbase/app-1:latest", "cloudbase/app-2:latest", "cloudbase/app-9:latest", "postgres:17")]

        def remove(self, tag):
            removed.append(tag)

    class Client:
        images = Images()
    monkeypatch.setattr(cleanup, "_get_client", lambda: Client())
    # app 1 exists, app 2 was deleted, app 9 was made after the list was read
    assert cleanup.remove_leftovers({1}, up_to=5) == {"images": 1, "sources": 2}
    assert removed == ["cloudbase/app-2:latest"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["app-1", "app-9", "something"]


def test_the_list_of_apps_covers_every_id_given_out(client):
    from workers import leftovers
    gone = _app()
    there = _app()
    from core.database import AsyncSessionLocal
    from core.models import Application

    async def delete():
        async with AsyncSessionLocal() as db:
            await db.delete(await db.get(Application, gone))
            await db.commit()
    _run(delete())
    _made["apps"].remove(gone)
    try:
        got = _run(leftovers.apps_there_are())
        assert there in got["keep"] and gone not in got["keep"] and got["up_to"] >= max(gone, there)
    finally:
        _clean_up()
