"""Moving a database to another node: dump it, start an empty one with the same credentials on the target,
load the dump, switch over. The data on the source node is only removed once the target works; when a
step fails the database goes back to the source node, which still has its data."""

import asyncio
import logging
from core.audit import log_audit
from core.database import AsyncSessionLocal
from core.models import Database, Node
from dbs import backup_restore as dbs_backup_restore, engines as db_engines, backups as dbs_backups, lifecycle as dbs_lifecycle, links as db_links
from dbs.ops import _run_on_node, _set_status
from dbs.provision import _spec
from dbs.public import sync_public_access
from dbs.state import PROVISION_TIMEOUT_SECONDS, _operation
from dbs.workers import _fetch_states

log = logging.getLogger("cloudbase.databases")

READY_TIMEOUT_SECONDS = 300


async def _wait_until_running(node: Node, database_id: int) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + READY_TIMEOUT_SECONDS
    state = "missing"
    while loop.time() < deadline:
        try:
            state = (await _fetch_states(node, [database_id])).get(str(database_id), {}).get("state", state)
        except Exception:
            pass
        if state == "running":
            return
        if state == "restarting":
            break
        await asyncio.sleep(3)
    raise RuntimeError(f"The new database on node '{node.name}' did not become ready (state: {state})")


_UNIT = {"postgres": "tables", "mysql": "tables", "mariadb": "tables", "mongodb": "collections", "redis": "keys", "valkey": "keys"}


async def _count(node: Node, spec: dict) -> int:
    result = await _run_on_node(node, lambda: dbs_backup_restore.count_objects(spec), "db_count", {"spec": spec})
    return int(result["count"]) if isinstance(result, dict) else int(result)


async def _set_node(database_id: int, node_id: int) -> None:
    async with AsyncSessionLocal() as db:
        database = await db.get(Database, database_id)
        database.node_id = node_id
        await db.commit()
        await db_links.refresh(db)


async def move_database(database_id: int, target_node_id: int, actor: str) -> None:
    """Runs with the database claimed (see the route); the caller's _spawn lets go and records a failure."""
    async with AsyncSessionLocal() as db:
        database = await db.get(Database, database_id)
        source = await db.get(Node, database.node_id)
        target = await db.get(Node, target_node_id)

    engine = db_engines.get_engine(database.engine)
    switched = False
    try:
        _operation[database_id] = "Checking the database before the move…"
        async with AsyncSessionLocal() as db:
            source_spec = _spec(await db.get(Database, database_id))
        before = await _count(source, source_spec)

        _operation[database_id] = "Backing up before the move…"
        backup_id = await dbs_backups.run_backup(database_id, "pre_move", actor)

        _operation[database_id] = f"Stopping on {source.name}…"
        await _run_on_node(source, lambda: dbs_lifecycle.stop(database_id), "db_stop", {"database_id": database_id})

        await _set_node(database_id, target.id)
        switched = True
        _operation[database_id] = f"Starting on {target.name}…"
        spec = {**source_spec, "initialized": False}        # an empty database, same credentials
        await _run_on_node(target, lambda: dbs_lifecycle.ensure_running(spec), "db_ensure", {"spec": spec},
                           timeout=PROVISION_TIMEOUT_SECONDS)
        await _wait_until_running(target, database_id)

        _operation[database_id] = f"Loading the data on {target.name}…"
        await dbs_backups._restore(database_id, backup_id, False, actor)
        restore = dbs_backups._restores.get(database_id) or {}
        if restore.get("state") != "success":
            raise RuntimeError(f"Loading the data failed: {restore.get('message')}")

        _operation[database_id] = "Checking the data on the new node…"
        after = await _count(target, spec)
        low = int(before * 0.9) if engine.count_approximate else before     # keys with an expiry may be gone by now
        if after < low or (after != before and not engine.count_approximate):
            raise RuntimeError(f"The new database holds {after} {_UNIT.get(engine.key, 'objects')} where the old one has {before}")
    except Exception as e:
        log.warning("[db] moving database %s to %s failed, going back to %s: %s", database_id, target.name, source.name, e)
        _operation[database_id] = f"Move failed, going back to {source.name}…"
        if switched:
            try:
                await _run_on_node(target, lambda: dbs_lifecycle.remove(database_id, keep_volume=False), "db_remove",
                                   {"database_id": database_id, "keep_volume": False})
            except Exception:
                log.warning("[db] could not clean up the new database on %s", target.name, exc_info=True)
            await _set_node(database_id, source.id)
        async with AsyncSessionLocal() as db:
            spec = _spec(await db.get(Database, database_id))
        try:
            result = await _run_on_node(source, lambda: dbs_lifecycle.ensure_running(spec), "db_ensure", {"spec": spec},
                                        timeout=PROVISION_TIMEOUT_SECONDS)
            await _set_status(database_id, "running" if result.get("state") == "running" else "starting")
        except Exception as back:
            await _set_status(database_id, "error", f"The move failed and the database could not be started again on {source.name}: {back}")
        raise RuntimeError(f"Moving to '{target.name}' failed, the database stays on '{source.name}': {e}")

    try:
        await _run_on_node(source, lambda: dbs_lifecycle.remove(database_id, keep_volume=False), "db_remove",
                           {"database_id": database_id, "keep_volume": False})
    except Exception as e:
        log.warning("[db] database %s moved, but its old data on %s was not removed: %s", database_id, source.name, e)
    await _set_status(database_id, "running")
    async with AsyncSessionLocal() as db:
        await log_audit(db, "database.move", actor=actor, detail={
            "database_id": database_id, "from": source.name, "to": target.name})
        await db.commit()
        await db_links.refresh(db)
    await sync_public_access()
