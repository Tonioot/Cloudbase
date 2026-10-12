"""Container management for managed databases. Runs on the primary and on nodes.

A database is one container (cloudbase-db-<id>) with one named volume
(cloudbase-db-<id>-data) on one node. The container is disposable; the volume holds
the data and is only ever removed by an explicit delete without "keep data".

All functions are synchronous (Docker SDK) and take a plain `spec` dict so the same
calls work locally and through the node agent:

    {database_id, name, engine, version, credentials, cpu_limit, memory_limit_mb, initialized}
"""

import logging
from typing import Optional

import db_engines
import docker_manager as dm

log = logging.getLogger("cloudbase.databases")

NETWORK = "cloudbase"
STOP_TIMEOUT_SECONDS = 60  # databases need time for a clean shutdown


class VolumeMissingError(RuntimeError):
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

    if not _volume_exists(client, db_id):
        if spec.get("initialized"):
            raise VolumeMissingError(
                f"Data volume {volume_name(db_id)} is missing on this node. "
                "Cloudbase does not create an empty database in its place; restore from a backup instead."
            )
        client.volumes.create(
            volume_name(db_id),
            labels={"cloudbase.database_id": str(db_id), "cloudbase.engine": spec["engine"]},
        )
        log.info("[db] Created volume %s", volume_name(db_id))

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
