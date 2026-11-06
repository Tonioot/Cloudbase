import logging
from typing import Optional
from dbs import engines as db_engines
from containers import stats_logs as containers_stats_logs
from dbs.docker import NETWORK, STOP_TIMEOUT_SECONDS, VolumeConflictError, VolumeMissingError, _client, _container_state, _get_container, _progress, _run_container, _volume_exists, container_name, volume_name

log = logging.getLogger("cloudbase.databases")


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
    data = containers_stats_logs.get_container_stats_by_name(container_name(database_id))
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
    return containers_stats_logs.get_recent_container_logs_by_name(container_name(database_id), lines)
