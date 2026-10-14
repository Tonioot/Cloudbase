"""Container management for managed databases. Runs on the primary and on nodes.

A database is one container (cloudbase-db-<id>) with one named volume
(cloudbase-db-<id>-data) on one node. The container is disposable; the volume holds
the data and is only ever removed by an explicit delete without "keep data".

All functions are synchronous (Docker SDK) and take a plain `spec` dict so the same
calls work locally and through the node agent:

    {database_id, name, engine, version, credentials, cpu_limit, memory_limit_mb, initialized}
"""

import gzip
import hashlib
import logging
import os
import shutil
import tarfile
import threading
import time
from typing import Optional

import db_engines
import docker_manager as dm

log = logging.getLogger("cloudbase.databases")

NETWORK = "cloudbase"
STOP_TIMEOUT_SECONDS = 60  # databases need time for a clean shutdown
# Creating or starting a container can take minutes on a busy small host (a
# Raspberry Pi extracting images); the default 60 s made Docker calls fail while
# Docker carried on, which led to duplicate attempts.
DOCKER_TIMEOUT_SECONDS = 300

_client_instance = None
_client_lock = threading.Lock()
# One image pull at a time per host: parallel pulls of several large images
# saturate disk and CPU on small hosts.
_pull_lock = threading.Lock()


def _client():
    global _client_instance
    if _client_instance is None:
        with _client_lock:
            if _client_instance is None:
                import docker  # type: ignore
                _client_instance = docker.from_env(timeout=DOCKER_TIMEOUT_SECONDS)
    return _client_instance


class VolumeMissingError(RuntimeError):
    pass


class VolumeConflictError(RuntimeError):
    pass


def container_name(database_id: int) -> str:
    return f"cloudbase-db-{database_id}"


def volume_name(database_id: int) -> str:
    return f"cloudbase-db-{database_id}-data"


def _get_container(client, database_id: int):
    import docker  # type: ignore
    try:
        return client.containers.get(container_name(database_id))
    except docker.errors.NotFound:
        return None


def _volume_exists(client, database_id: int) -> bool:
    import docker  # type: ignore
    try:
        client.volumes.get(volume_name(database_id))
        return True
    except docker.errors.NotFound:
        return False


def _container_state(container) -> str:
    """running | starting | unhealthy | restarting | stopped"""
    container.reload()
    state = container.attrs.get("State") or {}
    status = state.get("Status") or container.status
    if status == "running":
        health = (state.get("Health") or {}).get("Status")
        return {"starting": "starting", "unhealthy": "unhealthy"}.get(health, "running")
    if status == "restarting":
        return "restarting"
    return "stopped"


# What a database is doing before its container exists, e.g. "Pulling postgres:18 · 45%".
# Read by the primary (local databases) or pushed by the agent (remote ones).
_progress: dict[int, str] = {}


def progress(database_id: int) -> Optional[str]:
    return _progress.get(int(database_id))


def all_progress() -> dict[int, str]:
    return dict(_progress)


def _ensure_image(client, ref: str, database_id: int) -> None:
    import docker  # type: ignore
    try:
        client.images.get(ref)
        return
    except docker.errors.ImageNotFound:
        pass
    _progress[database_id] = "Waiting for another image download"
    with _pull_lock:
        try:
            client.images.get(ref)  # pulled meanwhile for another database
            return
        except docker.errors.ImageNotFound:
            pass
        _pull(client, ref, database_id)


def _pull(client, ref: str, database_id: int) -> None:
    log.info("[db] Pulling %s", ref)
    repo, _, tag = ref.rpartition(":")
    _progress[database_id] = f"Pulling {ref}"
    layers: dict[str, tuple[int, int]] = {}  # layer id → (downloaded, total)
    for event in client.api.pull(repo, tag=tag, stream=True, decode=True):
        if event.get("error"):
            raise RuntimeError(f"Pulling {ref} failed: {event['error']}")
        detail = event.get("progressDetail") or {}
        layer, status = event.get("id"), event.get("status", "")
        if layer and detail.get("total"):
            if status == "Downloading":
                layers[layer] = (detail.get("current", 0), detail["total"])
            elif status == "Extracting":
                _progress[database_id] = f"Extracting {ref}"
        elif layer and status in ("Download complete", "Pull complete") and layer in layers:
            layers[layer] = (layers[layer][1], layers[layer][1])
        if layers and not _progress[database_id].startswith("Extracting"):
            done = sum(c for c, _ in layers.values())
            total = sum(t for _, t in layers.values())
            _progress[database_id] = f"Pulling {ref} · {done * 100 // max(total, 1)}% of {total / 1e6:.0f} MB"
    log.info("[db] Pulled %s", ref)


def _run_container(client, spec: dict):
    engine = db_engines.get_engine(spec["engine"])
    version = spec["version"]
    creds = spec["credentials"]
    db_id = int(spec["database_id"])
    mem_mb = spec.get("memory_limit_mb")

    image = engine.image_ref(version)
    _ensure_image(client, image, db_id)
    _progress[db_id] = "Starting the container"
    dm.ensure_network(NETWORK)

    run_kwargs = {
        "detach": True,
        "name": container_name(db_id),
        "environment": engine.container_env(creds),
        "volumes": {volume_name(db_id): {"bind": engine.data_dir(version), "mode": "rw"}},
        "network": NETWORK,
        # No published ports: apps reach the database over the cloudbase network.
        "restart_policy": {"Name": "unless-stopped"},
        "stop_signal": "SIGTERM",
        "healthcheck": {
            "test": ["CMD-SHELL", engine.healthcheck],
            "interval": 10_000_000_000,
            "timeout": 5_000_000_000,
            "retries": 6,
            # The first start initialises the data directory, which takes minutes on a
            # busy or small host (MySQL on a Raspberry Pi: 3+ minutes). Failed checks in
            # this window don't count; a passing check still marks it healthy at once.
            "start_period": 600_000_000_000,
        },
        "security_opt": ["no-new-privileges:true"],
        "log_config": {"type": "json-file", "config": {"max-size": "10m", "max-file": "3"}},
        "labels": {
            "cloudbase.database_id": str(db_id),
            "cloudbase.database_name": spec.get("name") or "",
            "cloudbase.engine": engine.key,
            "cloudbase.version": version,
        },
    }
    command = engine.command(creds, mem_mb)
    if command:
        run_kwargs["command"] = command
    if spec.get("cpu_limit"):
        run_kwargs["nano_cpus"] = int(float(spec["cpu_limit"]) * 1_000_000_000)
    if mem_mb:
        run_kwargs["mem_limit"] = int(mem_mb) * 1024 * 1024

    import docker  # type: ignore
    try:
        return client.containers.run(image, **run_kwargs)
    except docker.errors.APIError as e:
        # On a slow host an earlier attempt can time out on our side while Docker still
        # creates the container. Take that container over instead of failing on the name.
        if e.status_code != 409:
            raise
        existing = _get_container(client, db_id)
        if existing is None:
            raise
        log.info("[db] %s already exists, taking it over", container_name(db_id))
        if _container_state(existing) == "stopped":
            existing.start()
        return existing


def ensure_running(spec: dict) -> dict:
    """Start the database, creating its container (and on first use its volume).

    Never initialises an empty database over a missing volume of a database that
    has been running before: that raises VolumeMissingError instead.
    """
    client = _client()
    db_id = int(spec["database_id"])

    container = _get_container(client, db_id)
    if container is not None:
        if _container_state(container) == "stopped":
            container.start()
        return {"container_id": container.id, "state": _container_state(container)}

    token = spec.get("volume_token") or ""
    if not _volume_exists(client, db_id):
        if spec.get("initialized"):
            raise VolumeMissingError(
                f"Data volume {volume_name(db_id)} is missing on this node. "
                "Cloudbase does not create an empty database in its place; restore from a backup instead."
            )
        client.volumes.create(
            volume_name(db_id),
            labels={"cloudbase.database_id": str(db_id), "cloudbase.engine": spec["engine"],
                    "cloudbase.volume_token": token},
        )
        log.info("[db] Created volume %s", volume_name(db_id))
    elif not spec.get("initialized"):
        # A new database must start empty. A volume left by another (deleted) database
        # would bring its data and its old credentials along.
        labels = client.volumes.get(volume_name(db_id)).attrs.get("Labels") or {}
        if labels.get("cloudbase.volume_token") != token:
            raise VolumeConflictError(
                f"Data volume {volume_name(db_id)} already exists on this node and belongs to an earlier "
                f"database. Remove it (docker volume rm {volume_name(db_id)}) or keep it and delete this database."
            )

    try:
        container = _run_container(client, spec)
    finally:
        _progress.pop(db_id, None)
    log.info("[db] Started %s (%s %s)", container_name(db_id), spec["engine"], spec["version"])
    return {"container_id": container.id, "state": _container_state(container)}


def recreate(spec: dict) -> dict:
    """Replace the container (e.g. after changing limits). The volume is kept."""
    client = _client()
    container = _get_container(client, int(spec["database_id"]))
    if container is not None:
        try:
            container.stop(timeout=STOP_TIMEOUT_SECONDS)
        except Exception:
            pass
        # v=True also drops anonymous volumes the image declares (e.g. MongoDB's
        # /data/configdb); the named data volume is never touched by it.
        container.remove(force=True, v=True)
    return ensure_running(spec)


def stop(database_id: int) -> dict:
    client = _client()
    container = _get_container(client, database_id)
    if container is not None and _container_state(container) != "stopped":
        container.stop(timeout=STOP_TIMEOUT_SECONDS)
    return {"state": "stopped" if container is not None else "missing"}


def restart(spec: dict) -> dict:
    client = _client()
    container = _get_container(client, int(spec["database_id"]))
    if container is None:
        return ensure_running(spec)
    container.restart(timeout=STOP_TIMEOUT_SECONDS)
    return {"container_id": container.id, "state": _container_state(container)}


def remove(database_id: int, keep_volume: bool = True) -> dict:
    import docker  # type: ignore
    client = _client()
    container = _get_container(client, database_id)
    if container is not None:
        try:
            container.stop(timeout=STOP_TIMEOUT_SECONDS)
        except Exception:
            pass
        # v=True also drops anonymous volumes the image declares (e.g. MongoDB's
        # /data/configdb); the named data volume is never touched by it.
        container.remove(force=True, v=True)
    volume_removed = False
    if not keep_volume:
        try:
            client.volumes.get(volume_name(database_id)).remove(force=True)
            volume_removed = True
        except docker.errors.NotFound:
            pass
    log.info("[db] Removed %s (volume %s)", container_name(database_id), "removed" if volume_removed else "kept")
    return {"ok": True, "volume_removed": volume_removed}


def states(database_ids: list[int]) -> dict[str, dict]:
    """{id: {state, volume}} where state is running|starting|unhealthy|restarting|stopped|missing."""
    client = _client()
    out: dict[str, dict] = {}
    for db_id in database_ids:
        container = _get_container(client, int(db_id))
        state = _container_state(container) if container is not None else "missing"
        out[str(db_id)] = {"state": state, "volume": _volume_exists(client, int(db_id))}
    return out


def container_address(database_id: int) -> tuple[str, int]:
    """IP on the cloudbase network and port of a running database container."""
    client = _client()
    container = _get_container(client, database_id)
    if container is None:
        raise RuntimeError(f"Database container {container_name(database_id)} does not exist on this node")
    container.reload()
    ip = ((container.attrs.get("NetworkSettings") or {}).get("Networks") or {}).get(NETWORK, {}).get("IPAddress")
    if not ip:
        raise RuntimeError(f"{container_name(database_id)} is not running")
    engine = db_engines.get_engine(container.labels.get("cloudbase.engine", ""))
    return ip, engine.port


def stats(database_id: int, data_dir: Optional[str] = None) -> dict:
    data = dm.get_container_stats_by_name(container_name(database_id))
    if data and data_dir:
        try:
            client = _client()
            container = _get_container(client, database_id)
            result = container.exec_run(["du", "-sk", data_dir]) if container is not None else None
            if result is not None and result.exit_code == 0:
                data["disk_mb"] = round(int(result.output.split()[0]) / 1024, 1)
        except Exception:
            log.debug("[db] du failed for %s", container_name(database_id), exc_info=True)
    return data


def stats_many(items: list[dict]) -> dict[str, dict]:
    """Stats for several databases: items are {database_id, data_dir or None}.
    Measuring the data size (du) is slower, so callers ask for it only now and then."""
    out: dict[str, dict] = {}
    for item in items:
        try:
            data = stats(int(item["database_id"]), item.get("data_dir"))
        except Exception:
            data = {}
        if data:
            out[str(item["database_id"])] = data
    return out


def logs(database_id: int, lines: int = 300) -> list[str]:
    return dm.get_recent_container_logs_by_name(container_name(database_id), lines)


# ── Backups ───────────────────────────────────────────────────────────────────
# Dumps and restores run inside the database container (its own client tools,
# its own credentials from the environment). Files are streamed, never held in
# memory, so large databases work.

RESTORE_PATH = "/tmp/cb-restore"


def _running_container(database_id: int):
    container = _get_container(_client(), database_id)
    if container is None or _container_state(container) == "stopped":
        raise RuntimeError("The database must be running to back it up or restore it")
    return container


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _tail(data: bytes, limit: int = 1500) -> str:
    text = data.decode("utf-8", errors="replace").strip()
    return text[-limit:] if len(text) > limit else text


def backup_to_file(spec: dict, out_path: str) -> dict:
    """Dump the database to `out_path`. Returns {size_bytes, sha256}."""
    engine = db_engines.get_engine(spec["engine"])
    if not engine.backup:
        raise RuntimeError(f"{engine.label} backups are not supported")
    container = _running_container(int(spec["database_id"]))
    api = container.client.api

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    part = out_path + ".part"
    exec_id = api.exec_create(container.id, ["sh", "-c", engine.backup["dump"]], stdout=True, stderr=True)["Id"]
    stderr = bytearray()
    try:
        with open(part, "wb") as raw:
            out = gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6, mtime=0) if engine.backup["gzip"] else raw
            for chunk_out, chunk_err in api.exec_start(exec_id, stream=True, demux=True):
                if chunk_out:
                    out.write(chunk_out)
                if chunk_err:
                    stderr += chunk_err
                    del stderr[:-8192]
            if out is not raw:
                out.close()
        code = api.exec_inspect(exec_id).get("ExitCode")
        if code != 0:
            raise RuntimeError(f"Dump failed (exit {code}): {_tail(bytes(stderr)) or 'no output'}")
        os.replace(part, out_path)
    finally:
        if os.path.exists(part):
            os.remove(part)
    os.chmod(out_path, 0o600)  # a dump holds all data
    return {"size_bytes": os.path.getsize(out_path), "sha256": _sha256(out_path)}


def _tar_stream(path: str, name: str):
    """A one-file tar archive, generated on the fly for put_archive."""
    size = os.path.getsize(path)
    info = tarfile.TarInfo(name=name)
    info.size = size
    info.mode = 0o644
    info.mtime = int(time.time())
    yield info.tobuf(format=tarfile.GNU_FORMAT)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            yield chunk
    yield b"\0" * ((512 - size % 512) % 512) + b"\0" * 1024


def restore_from_file(spec: dict, in_path: str, source_db_name: Optional[str] = None) -> dict:
    """Load a dump into the database, replacing what the dump contains."""
    engine = db_engines.get_engine(spec["engine"])
    if engine.restore_via_helper:
        return _restore_with_helper(spec, engine, in_path)
    if engine.restore is None:
        raise RuntimeError(f"Restoring {engine.label} backups is not supported yet; download the backup instead")
    container = _running_container(int(spec["database_id"]))
    api = container.client.api

    with open(in_path, "rb") as f:
        head = f.read(8)
    is_gzip = head[:2] == b"\x1f\x8b"
    prepared = in_path
    unpacked = None
    try:
        if is_gzip and engine.key != "mongodb":  # mongorestore reads gzip itself
            unpacked = in_path + ".unpacked"
            with gzip.open(in_path, "rb") as src, open(unpacked, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            prepared, is_gzip = unpacked, False
            with open(prepared, "rb") as f:
                head = f.read(8)

        container.put_archive(os.path.dirname(RESTORE_PATH), _tar_stream(prepared, os.path.basename(RESTORE_PATH)))
        ctx = {
            "path": RESTORE_PATH,
            "gzip": is_gzip,
            "pg_custom": head.startswith(b"PGDMP"),
            "ns_from": source_db_name,
            "ns_to": spec["credentials"].get("db_name"),
        }
        cmd = f"{engine.restore(ctx)}; rc=$?; rm -f {RESTORE_PATH}; exit $rc"
        exec_id = api.exec_create(container.id, ["sh", "-c", cmd], stdout=True, stderr=True)["Id"]
        out, err = api.exec_start(exec_id, demux=True)
        code = api.exec_inspect(exec_id).get("ExitCode")
    finally:
        if unpacked and os.path.exists(unpacked):
            os.remove(unpacked)
    if code != 0:
        raise RuntimeError(f"Restore failed (exit {code}): {_tail((err or b'') + (out or b'')) or 'no output'}")
    return {"ok": True}


# ── Password rotation ─────────────────────────────────────────────────────────

# Engines whose password also lives in the container's command or environment in
# a way that matters (startup, backups); their container is recreated afterwards.
ROTATE_RECREATES = {"mongodb", "redis", "valkey"}


def _rotate_command(engine: str, user: str, new_password: str) -> Optional[str]:
    # token_urlsafe passwords contain only [A-Za-z0-9_-]; names only [a-z0-9_].
    if engine == "postgres":
        return f'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "ALTER USER \\"{user}\\" WITH PASSWORD \'{new_password}\'"'
    if engine == "mysql":
        return f'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot -e "ALTER USER \'{user}\'@\'%\' IDENTIFIED BY \'{new_password}\'"'
    if engine == "mariadb":
        return f'MYSQL_PWD="$MARIADB_ROOT_PASSWORD" mariadb -uroot -e "ALTER USER \'{user}\'@\'%\' IDENTIFIED BY \'{new_password}\'"'
    if engine == "mongodb":
        return ('mongosh --quiet -u "$MONGO_INITDB_ROOT_USERNAME" -p "$MONGO_INITDB_ROOT_PASSWORD" '
                f'--authenticationDatabase admin admin --eval "db.changeUserPassword(\'{user}\', \'{new_password}\')"')
    return None  # Redis / Valkey: the password is part of the server command


def rotate_password(spec: dict, new_password: str) -> dict:
    """Change the app user's password inside the running database (old credentials
    in `spec`). The caller stores the new password and, for ROTATE_RECREATES
    engines, recreates the container with it."""
    cmd = _rotate_command(spec["engine"], spec["credentials"]["user"], new_password)
    if cmd is None:
        return {"ok": True, "applied": "on recreate"}
    container = _running_container(int(spec["database_id"]))
    api = container.client.api
    exec_id = api.exec_create(container.id, ["sh", "-c", cmd], stdout=True, stderr=True)["Id"]
    out, err = api.exec_start(exec_id, demux=True)
    code = api.exec_inspect(exec_id).get("ExitCode")
    if code != 0:
        raise RuntimeError(f"Changing the password failed (exit {code}): {_tail((err or b'') + (out or b''))}")
    return {"ok": True, "applied": "in place"}


# ── Redis / Valkey restore ────────────────────────────────────────────────────
# These load their data only at startup, and with append-only persistence they
# ignore an RDB file that sits next to an existing AOF. So: stop the database, and
# in a helper container (same image, same volume, no network) load the RDB into a
# temporary server, let it write a fresh AOF, shut it down cleanly, start again.

_HELPER_SCRIPT = r"""
set -u
cd /data
rollback() {
  echo "restore failed, putting the previous data back" >&2
  rm -rf appendonlydir dump.rdb
  [ -d appendonlydir.before-restore ] && mv appendonlydir.before-restore appendonlydir
  [ -f dump.rdb.before-restore ] && mv dump.rdb.before-restore dump.rdb
  exit 1
}
rm -rf appendonlydir.before-restore dump.rdb.before-restore
[ -d appendonlydir ] && mv appendonlydir appendonlydir.before-restore
[ -f dump.rdb ] && mv dump.rdb dump.rdb.before-restore
mv restore.rdb dump.rdb || rollback
SERVER --dir /data --dbfilename dump.rdb --appendonly no --save '' --port 6399 --bind 127.0.0.1 --daemonize yes || rollback
i=0
until CLI -p 6399 ping 2>/dev/null | grep -q PONG; do i=$((i+1)); [ $i -gt 1200 ] && rollback; sleep 0.5; done
echo "loaded $(CLI -p 6399 dbsize) keys"
CLI -p 6399 config set appendonly yes >/dev/null || rollback
sleep 1
i=0
until CLI -p 6399 info persistence | grep -q 'aof_rewrite_in_progress:0' \
   && CLI -p 6399 info persistence | grep -q 'aof_rewrite_scheduled:0' \
   && ls appendonlydir/*.manifest >/dev/null 2>&1; do
  i=$((i+1)); [ $i -gt 2400 ] && rollback; sleep 0.5
done
CLI -p 6399 shutdown >/dev/null 2>&1
i=0
while CLI -p 6399 ping >/dev/null 2>&1; do i=$((i+1)); [ $i -gt 120 ] && rollback; sleep 0.5; done
rm -rf appendonlydir.before-restore dump.rdb.before-restore
echo "restore complete"
"""


def _restore_with_helper(spec: dict, engine, in_path: str) -> dict:
    import docker  # type: ignore
    db_id = int(spec["database_id"])
    client = _client()
    main = _get_container(client, db_id)
    if main is None:
        raise RuntimeError("The database container does not exist; start the database first")

    with open(in_path, "rb") as f:
        is_gzip = f.read(2) == b"\x1f\x8b"
    rdb = in_path
    if is_gzip:
        rdb = in_path + ".rdb"
        with gzip.open(in_path, "rb") as src, open(rdb, "wb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
    with open(rdb, "rb") as f:
        if f.read(5) != b"REDIS":
            raise RuntimeError("This is not a Redis/Valkey RDB file")

    script = _HELPER_SCRIPT.replace("SERVER", engine.restore_via_helper["server"]).replace("CLI", engine.restore_via_helper["cli"])
    helper_name = f"{container_name(db_id)}-restore"
    try:
        old = client.containers.get(helper_name)
        old.remove(force=True, v=True)
    except docker.errors.NotFound:
        pass

    main.stop(timeout=STOP_TIMEOUT_SECONDS)
    helper = None
    try:
        helper = client.containers.create(
            main.image.id,
            name=helper_name,
            entrypoint=["sh", "-c", script],
            command=[],
            volumes={volume_name(db_id): {"bind": "/data", "mode": "rw"}},
            network_mode="none",
            labels={"cloudbase.database_id": str(db_id), "cloudbase.role": "restore-helper"},
        )
        helper.put_archive("/data", _tar_stream(rdb, "restore.rdb"))
        helper.start()
        result = helper.wait(timeout=3600)
        output = helper.logs(stdout=True, stderr=True)
        if result.get("StatusCode") != 0:
            raise RuntimeError(f"Restore failed: {_tail(output)}")
        log.info("[db] %s restored: %s", container_name(db_id), _tail(output, 200))
    finally:
        if helper is not None:
            try:
                helper.remove(force=True, v=True)
            except Exception:
                pass
        if rdb != in_path and os.path.exists(rdb):
            os.remove(rdb)
        main.start()  # with the restored data, or the previous data after a rollback
    return {"ok": True}
