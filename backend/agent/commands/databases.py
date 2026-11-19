import os
import asyncio
import hashlib
import threading
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
_UPLOAD_PATH = "/api/nodes/agent/databases/{database_id}/backups/{backup_id}/upload"


async def _reachable(client, url: str) -> bool:
    """Whether something answers at this address (any answer will do): a probe before the dump starts."""
    try:
        await client.get(url, timeout=5)
        return True
    except Exception:
        return False


async def _upload_base(client, state, hub_url) -> str:
    """Where the backup goes: the primary over the node network when that works (private, no proxy in between),
    otherwise the panel address."""
    if hub_url and await _reachable(client, hub_url + "/api/health"):
        return hub_url
    return _normalize_url(state.main_url)


class _StreamSink:
    """What the dump writes into: hands the chunks to the upload, counting and hashing them on the way. The dump
    waits when the upload is slower (a bounded queue), so nothing piles up in memory or on disk."""

    def __init__(self, loop, queue, stop):
        self._loop, self._queue, self._stop = loop, queue, stop
        self.size, self.hash = 0, hashlib.sha256()

    def put(self, item) -> None:
        asyncio.run_coroutine_threadsafe(self._queue.put(item), self._loop).result()

    def write(self, data) -> int:
        if self._stop.is_set():
            raise RuntimeError("The upload stopped")
        data = bytes(data)
        self.size += len(data)
        self.hash.update(data)
        self.put(data)
        return len(data)

    def flush(self) -> None:
        pass


async def cmd_db_backup(client, state, _main_id, payload):
    """Dump a database on this node straight into the upload to the primary: no copy is kept on this node. The
    primary only keeps the file when the stream ends cleanly, so a dump that fails halfway leaves nothing behind."""
    spec, backup_id = payload["spec"], int(payload["backup_id"])
    base = await _upload_base(client, state, payload.get("hub_url"))
    url = base + _UPLOAD_PATH.format(database_id=spec["database_id"], backup_id=backup_id)

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=8)
    stop = threading.Event()
    sink = _StreamSink(loop, queue, stop)

    def dump():
        try:
            dbs_backup_restore.dump_to_stream(spec, sink)
            sink.put(None)
        except BaseException as e:
            if not stop.is_set():
                sink.put(e)

    async def body():
        while True:
            item = await queue.get()
            if item is None:
                return
            if isinstance(item, BaseException):
                raise item
            yield item

    dumping = asyncio.create_task(asyncio.to_thread(dump))
    try:
        resp = await client.post(url, content=body(), headers={"X-Node-Token": state.auth_token}, timeout=None)
        resp.raise_for_status()
    finally:
        stop.set()
        while not queue.empty():          # a dump waiting to hand over a chunk must be let go
            queue.get_nowait()
        await dumping
    return {"size_bytes": sink.size, "sha256": sink.hash.hexdigest()}


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


async def cmd_db_count(_client, _state, _main_id, payload):
    return {"count": await asyncio.to_thread(dbs_backup_restore.count_objects, payload["spec"])}
