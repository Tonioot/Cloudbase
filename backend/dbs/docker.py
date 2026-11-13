import logging
import threading
from typing import Optional
from dbs import engines as db_engines
from containers import client as containers_client, naming
from core import config as _cfg
from agent import state as agent_state

log = logging.getLogger("cloudbase.databases")


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
    return naming.database_container_name(database_id)


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


def published_port(database_id: int) -> int:
    """The port a database publishes on its node's address on the node network."""
    port = _cfg.get_port("db_proxy_min") + int(database_id)
    if port > _cfg.get_port("db_proxy_max"):
        raise RuntimeError(f"Database id {database_id} is outside the db_proxy port range in config.yaml")
    return port


def publish_address() -> Optional[str]:
    """This machine's address on the node network, where its databases publish their port: reachable by the other
    nodes and nowhere else. None while there is none (no node network); apps on other nodes cannot connect then."""
    state = agent_state._load_state()
    if state is not None:
        return state.wg_ip or None
    from mesh import hub, addressing
    return addressing.hub_ip(_cfg.get_wireguard()["subnet"]) if hub._hub["up"] else None


def published_binding(container) -> Optional[tuple[str, int]]:
    """(address, port) the container publishes its database port on, or None."""
    for bindings in ((container.attrs.get("HostConfig") or {}).get("PortBindings") or {}).values():
        for b in bindings or []:
            return b.get("HostIp") or "", int(b.get("HostPort") or 0)
    return None


def _run_container(client, spec: dict):
    engine = db_engines.get_engine(spec["engine"])
    version = spec["version"]
    creds = spec["credentials"]
    db_id = int(spec["database_id"])
    mem_mb = spec.get("memory_limit_mb")

    image = spec.get("image_override") or engine.image_ref(version)
    if not spec.get("image_override"):
        _ensure_image(client, image, db_id)
    _progress[db_id] = "Starting the container"
    network = containers_client.ensure_database_network(db_id)      # the apps linked to it join this one

    run_kwargs = {
        "detach": True,
        "name": container_name(db_id),
        "environment": engine.container_env(creds),
        "volumes": {volume_name(db_id): {"bind": engine.data_dir(version), "mode": "rw"}},
        "network": network,
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
    address = publish_address()
    if address:      # apps on other nodes (and the primary's public access) connect here; see dbs/proxy.py
        run_kwargs["ports"] = {f"{engine.port}/tcp": (address, published_port(db_id))}
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
