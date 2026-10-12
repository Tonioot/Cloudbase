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
import time
from typing import Optional

import db_engines
import docker_manager as dm

log = logging.getLogger("cloudbase.databases")

NETWORK = "cloudbase"
STOP_TIMEOUT_SECONDS = 60  # databases need time for a clean shutdown


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


def _ensure_image(client, ref: str) -> None:
    import docker  # type: ignore
    try:
        client.images.get(ref)
    except docker.errors.ImageNotFound:
        log.info("[db] Pulling %s", ref)
        repo, _, tag = ref.rpartition(":")
        client.images.pull(repo, tag=tag)


def _run_container(client, spec: dict):
    engine = db_engines.get_engine(spec["engine"])
    version = spec["version"]
    creds = spec["credentials"]
    db_id = int(spec["database_id"])
    mem_mb = spec.get("memory_limit_mb")

    image = engine.image_ref(version)
    _ensure_image(client, image)
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
            "start_period": 60_000_000_000,
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

    return client.containers.run(image, **run_kwargs)


def ensure_running(spec: dict) -> dict:
    """Start the database, creating its container (and on first use its volume).

    Never initialises an empty database over a missing volume of a database that
    has been running before: that raises VolumeMissingError instead.
    """
    client = dm._get_client()
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

    container = _run_container(client, spec)
    log.info("[db] Started %s (%s %s)", container_name(db_id), spec["engine"], spec["version"])
    return {"container_id": container.id, "state": _container_state(container)}


def recreate(spec: dict) -> dict:
    """Replace the container (e.g. after changing limits). The volume is kept."""
    client = dm._get_client()
    container = _get_container(client, int(spec["database_id"]))
    if container is not None:
        try:
            container.stop(timeout=STOP_TIMEOUT_SECONDS)
        except Exception:
            pass
        container.remove(force=True)
    return ensure_running(spec)


def stop(database_id: int) -> dict:
    client = dm._get_client()
    container = _get_container(client, database_id)
    if container is not None and _container_state(container) != "stopped":
        container.stop(timeout=STOP_TIMEOUT_SECONDS)
    return {"state": "stopped" if container is not None else "missing"}


def restart(spec: dict) -> dict:
    client = dm._get_client()
    container = _get_container(client, int(spec["database_id"]))
    if container is None:
        return ensure_running(spec)
    container.restart(timeout=STOP_TIMEOUT_SECONDS)
    return {"container_id": container.id, "state": _container_state(container)}


def remove(database_id: int, keep_volume: bool = True) -> dict:
    import docker  # type: ignore
    client = dm._get_client()
    container = _get_container(client, database_id)
    if container is not None:
        try:
            container.stop(timeout=STOP_TIMEOUT_SECONDS)
        except Exception:
            pass
        container.remove(force=True)
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
    client = dm._get_client()
    out: dict[str, dict] = {}
    for db_id in database_ids:
        container = _get_container(client, int(db_id))
        state = _container_state(container) if container is not None else "missing"
        out[str(db_id)] = {"state": state, "volume": _volume_exists(client, int(db_id))}
    return out


def container_address(database_id: int) -> tuple[str, int]:
    """IP on the cloudbase network and port of a running database container."""
    client = dm._get_client()
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
            client = dm._get_client()
            container = _get_container(client, database_id)
            result = container.exec_run(["du", "-sk", data_dir]) if container is not None else None
            if result is not None and result.exit_code == 0:
                data["disk_mb"] = round(int(result.output.split()[0]) / 1024, 1)
        except Exception:
            log.debug("[db] du failed for %s", container_name(database_id), exc_info=True)
    return data


def logs(database_id: int, lines: int = 300) -> list[str]:
    return dm.get_recent_container_logs_by_name(container_name(database_id), lines)


# ── Backups ───────────────────────────────────────────────────────────────────
# Dumps and restores run inside the database container (its own client tools,
# its own credentials from the environment). Files are streamed, never held in
# memory, so large databases work.

RESTORE_PATH = "/tmp/cb-restore"


def _running_container(database_id: int):
    container = _get_container(dm._get_client(), database_id)
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
