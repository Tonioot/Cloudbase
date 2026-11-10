"""One operation at a time per database (dbs/state.py claim/release): checking and marking happen in one step,
so two clicks (or a click and the monitor) can never both start something on the same database."""
import asyncio

import pytest
from fastapi import HTTPException

from dbs import state


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean():
    state._busy.clear()
    yield
    state._busy.clear()


def test_a_second_claim_is_refused_and_does_not_take_over():
    state.claim(1, "Backing up…")
    with pytest.raises(HTTPException) as err:
        state.claim(1)
    assert err.value.status_code == 409 and 1 in state._busy and state._operation[1] == "Backing up…"
    state.release(1)
    assert 1 not in state._busy and 1 not in state._operation and state._released_at[1] > 0


def test_held_lets_go_when_the_operation_fails():
    with pytest.raises(RuntimeError):
        with state.held(2):
            raise RuntimeError("boom")
    assert 2 not in state._busy


@pytest.fixture
def database(client):
    from sqlalchemy import delete
    from core.database import AsyncSessionLocal
    from core.env_crypto import encrypt_text
    from core.models import Database, Node
    from nodes.registry import ensure_local_node
    made = []

    def make(name, node_offline=False):
        async def go():
            async with AsyncSessionLocal() as db:
                if node_offline:
                    node = Node(name=f"{name}-node", status="offline", auth_token=f"token-{name}", is_local=False, enabled=True)
                    db.add(node)
                    await db.flush()
                else:
                    node = await ensure_local_node(db)
                row = Database(name=name, engine="postgres", version="16", node_id=node.id, status="running",
                               desired_state="running", credentials=encrypt_text('{"user": "u", "password": "p", "db_name": "d"}'))
                db.add(row)
                await db.commit()
                made.append((row.id, node.id if node_offline else None))
                return row.id
        return _run(go())
    yield make

    async def cleanup():
        async with AsyncSessionLocal() as db:
            await db.execute(delete(Database).where(Database.id.in_([d for d, _ in made])))
            await db.execute(delete(Node).where(Node.id.in_([n for _, n in made if n])))
            await db.commit()
    _run(cleanup())


ACTIONS = [
    ("post", "/start", None), ("post", "/restart", None), ("post", "/stop", None),
    ("post", "/rotate-password", None), ("post", "/update", None),
    ("patch", "", {"cpu_limit": 1.5}), ("delete", "", None),
    ("post", "/backups", None), ("post", "/restore", {"backup_id": 1}),
]


@pytest.mark.parametrize("method,path,body", ACTIONS, ids=[f"{m} {p or '/'}" for m, p, _ in ACTIONS])
def test_every_action_on_a_busy_database_is_refused_and_leaves_the_other_operation_alone(admin, database, method, path, body):
    db_id = database(f"lock{abs(hash((method, path))) % 10**5}")
    state.claim(db_id, "Restoring backup…")
    r = admin.request(method.upper(), f"/api/databases/{db_id}{path}", json=body)
    assert r.status_code == 409, r.text
    assert db_id in state._busy and state._operation[db_id] == "Restoring backup…"


def test_a_database_on_an_offline_node_is_not_left_busy(admin, database):
    db_id = database("lock-offline", node_offline=True)
    for path in ("/start", "/stop", "/restart"):
        assert admin.post(f"/api/databases/{db_id}{path}").status_code == 409
        assert db_id not in state._busy, path


def test_the_monitor_does_not_start_a_database_somebody_else_holds(database, monkeypatch):
    """The monitor checks first and starts later; claim() in between makes sure nobody got in."""
    from dbs import workers
    db_id = database("lock-monitor")
    started = []
    monkeypatch.setattr(workers, "_spawn", lambda i, fn: started.append(i))

    async def states(node, ids):
        state.claim(db_id, "Updating…")                   # an operation starts while the monitor looks
        return {str(i): {"state": "stopped", "volume": True} for i in ids}
    monkeypatch.setattr(workers, "_fetch_states", states)
    monkeypatch.setattr(workers, "_last_heal", {})
    _run(workers._monitor_tick())
    assert started == [] and state._operation[db_id] == "Updating…"


def test_a_database_the_monitor_starts_is_held_while_it_starts(database, monkeypatch):
    """Otherwise a click could start a second operation on it in the meantime."""
    from dbs import workers
    database("lock-heal")
    held_while_started = []
    monkeypatch.setattr(workers, "_spawn", lambda i, fn: held_while_started.append(i in state._busy))

    async def states(node, ids):
        return {str(i): {"state": "stopped", "volume": True} for i in ids}
    monkeypatch.setattr(workers, "_fetch_states", states)
    monkeypatch.setattr(workers, "_last_heal", {})
    _run(workers._monitor_tick())
    assert held_while_started == [True]
