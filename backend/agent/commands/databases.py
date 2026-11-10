import os
import asyncio
from dbs import lifecycle as dbs_lifecycle
from dbs import backup_restore as dbs_backup_restore
from dbs import updates as dbs_updates
from agent.urls import _normalize_url

# ─── Managed databases ────────────────────────────────────────────────────────
# Thin wrappers: db_manager does the work, the primary keeps the state.

async def cmd_db_ensure(_client, _state, _main_id, payload):
    return await asyncio.to_thread(dbs_lifecycle.ensure_running, payload["spec"])


async def cmd_db_recreate(_client, _state, _main_id, payload):
    return await asyncio.to_thread(dbs_lifecycle.recreate, payload["spec"])


async def cmd_db_restart(_client, _state, _main_id, payload):
    return await asyncio.to_thread(dbs_lifecycle.restart, payload["spec"])


async def cmd_db_stop(_client, _state, _main_id, payload):
    return await asyncio.to_thread(dbs_lifecycle.stop, int(payload["database_id"]))


async def cmd_db_remove(_client, _state, _main_id, payload):
    return await asyncio.to_thread(
        dbs_lifecycle.remove, int(payload["database_id"]), bool(payload.get("keep_volume", True))
    )


async def cmd_db_stats(_client, _state, _main_id, payload):
    return await asyncio.to_thread(dbs_lifecycle.stats, int(payload["database_id"]), payload.get("data_dir"))


_BACKUP_TMP = os.path.expanduser("~/.cloudbase/backup-tmp")


async def cmd_db_backup(client, state, _main_id, payload):
    """Dump a database on this node and stream the file to the primary."""
    spec, backup_id = payload["spec"], int(payload["backup_id"])
    os.makedirs(_BACKUP_TMP, exist_ok=True)
    path = os.path.join(_BACKUP_TMP, f"backup-{backup_id}")
    try:
        info = await asyncio.to_thread(dbs_backup_restore.backup_to_file, spec, path)

        async def body():
            with open(path, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    yield chunk

        resp = await client.post(
            f"{_normalize_url(state.main_url)}/api/nodes/agent/databases/{spec['database_id']}/backups/{backup_id}/upload",
            content=body(), headers={"X-Node-Token": state.auth_token}, timeout=None,
        )
        resp.raise_for_status()
        return info
    finally:
        if os.path.exists(path):
            os.remove(path)


async def cmd_db_restore(client, state, _main_id, payload):
    """Fetch a backup from the primary and load it into a database on this node."""
    spec, backup_id = payload["spec"], int(payload["backup_id"])
    os.makedirs(_BACKUP_TMP, exist_ok=True)
    path = os.path.join(_BACKUP_TMP, f"restore-{backup_id}")
    try:
        async with client.stream(
            "GET",
            f"{_normalize_url(state.main_url)}/api/nodes/agent/databases/{spec['database_id']}/backups/{backup_id}/download",
            headers={"X-Node-Token": state.auth_token}, timeout=None,
        ) as resp:
            resp.raise_for_status()
            with open(path, "wb") as f:
                async for chunk in resp.aiter_bytes(1024 * 1024):
                    f.write(chunk)
        return await asyncio.to_thread(dbs_backup_restore.restore_from_file, spec, path, payload.get("source_db_name"))
    finally:
        if os.path.exists(path):
            os.remove(path)


async def cmd_db_rotate_password(_client, _state, _main_id, payload):
    return await asyncio.to_thread(dbs_backup_restore.rotate_password, payload["spec"], payload["new_password"])


async def cmd_db_check_updates(_client, _state, _main_id, payload):
    return {"updates": await asyncio.to_thread(dbs_updates.check_updates, payload.get("items") or [])}


async def cmd_db_update(_client, _state, _main_id, payload):
    return await asyncio.to_thread(dbs_updates.update, payload["spec"])


async def cmd_db_logs(_client, _state, _main_id, payload):
    lines = await asyncio.to_thread(dbs_lifecycle.logs, int(payload["database_id"]), int(payload.get("lines", 300)))
    return {"lines": lines}
