import logging
import re
import threading
from dbs import proxy as db_proxy
from containers import naming

log = logging.getLogger("cloudbase.docker")


# ── Lazy Docker client ────────────────────────────────────────────────────────

_docker_client = None


_docker_lock = threading.Lock()


def _get_client():
    global _docker_client
    if _docker_client is not None:
        return _docker_client
    with _docker_lock:
        if _docker_client is not None:
            return _docker_client
        import docker # type: ignore
        _docker_client = docker.from_env()
        return _docker_client


def _assert_image_local(client, img: str) -> None:
    """Raise RuntimeError if the image is not present locally, preventing docker-py
    from silently falling through to a Docker Hub pull attempt."""
    import docker # type: ignore
    try:
        client.images.get(img)
    except docker.errors.ImageNotFound:
        raise RuntimeError(
            f"Image '{img}' not found locally. Deploy the app first to build the image."
        )


_network_lock = threading.Lock()


def ensure_network(name: str, bridge: str, isolated: bool = False) -> None:
    """Create the user-defined bridge network if it does not exist yet. A fixed bridge name keeps firewall rules
    simple. `isolated`: the containers on it cannot reach each other (Docker's inter-container communication off)."""
    import docker  # type: ignore
    client = _get_client()
    with _network_lock:
        try:
            client.networks.get(name)
        except docker.errors.NotFound:
            options = {"com.docker.network.bridge.name": bridge}
            if isolated:
                options["com.docker.network.bridge.enable_icc"] = "false"
            client.networks.create(name, driver="bridge", labels={"cloudbase.managed": "true"}, options=options)
            log.info("[docker] Created network %s", name)


def ensure_apps_network() -> None:
    ensure_network(naming.APPS_NETWORK, "cbapps0", isolated=True)


def ensure_database_network(database_id: int) -> str:
    name = naming.database_network_name(database_id)
    ensure_network(name, f"cbdb{database_id}")
    return name


def attach(client, network: str, container) -> None:
    """Connect a container to a network, unless it already is."""
    container.reload()
    if network not in ((container.attrs.get("NetworkSettings") or {}).get("Networks") or {}):
        client.networks.get(network).connect(container)


def _join_database_networks(client, linked: list[dict]) -> list[str]:
    """The networks of the linked databases that run here, with each database container on its own network."""
    import docker  # type: ignore
    names = []
    for database_id in db_proxy.local_database_ids(linked):
        name = ensure_database_network(database_id)
        try:      # a database container made before this network existed is moved onto it here
            attach(client, name, client.containers.get(naming.database_container_name(database_id)))
        except docker.errors.NotFound:
            pass  # not started yet: it is created on its own network
        names.append(name)
    return names


def retire_legacy_network(client) -> None:
    """The shared network of earlier versions goes once only database containers are left on it: they are on their
    own networks by then, and no app container can reach another one through it."""
    import docker  # type: ignore
    try:
        legacy = client.networks.get(naming.LEGACY_NETWORK)
    except docker.errors.NotFound:
        return
    try:
        legacy.reload()
        databases = {}
        for entry in (legacy.attrs.get("Containers") or {}).values():
            found = re.fullmatch(r"cloudbase-db-(\d+)", entry.get("Name") or "")
            if not found:
                return                  # an app that has not been restarted since the update is still on it
            databases[entry["Name"]] = int(found.group(1))
        for name, database_id in databases.items():
            container = client.containers.get(name)
            attach(client, ensure_database_network(database_id), container)
            legacy.disconnect(container)
        legacy.remove()
        log.info("[docker] Removed the old shared network %s", naming.LEGACY_NETWORK)
    except Exception:
        log.warning("[docker] Could not remove the old shared network", exc_info=True)


def start_container(client, img: str, run_kwargs: dict, extra_networks: list[str]):
    """Run a container on its network and on the extra ones, which Docker can only add once it exists."""
    if not extra_networks:
        return client.containers.run(img, **run_kwargs)
    container = client.containers.create(img, **{k: v for k, v in run_kwargs.items() if k != "detach"})
    try:
        for name in extra_networks:
            attach(client, name, container)
        container.start()
    except Exception:
        container.remove(force=True)
        raise
    return container


def _host_port(external_port: int, docker_options: dict):
    """Docker port binding: on localhost only when docker_options asks for it."""
    bind_host = (docker_options or {}).get("bind_host")
    return (bind_host, external_port) if bind_host else external_port


def _apply_linked_databases(client, run_kwargs: dict, docker_options: dict) -> list[str]:
    """Put the container on the apps network, where it cannot reach other apps, and add the URLs of linked
    databases: direct on the database's node, over the node network elsewhere. The link wins when the app has an
    env var of the same name (an imported .env must not silently cut the connection). Returns the extra networks
    to join: those of the databases linked here."""
    ensure_apps_network()
    run_kwargs["network"] = naming.APPS_NETWORK
    linked = docker_options.get("linked_databases") or []
    if not linked:
        retire_legacy_network(client)
        return []
    linked_env = db_proxy.resolve_links(linked)
    run_kwargs["environment"] = {**(run_kwargs.get("environment") or {}), **linked_env}
    networks = _join_database_networks(client, linked)
    retire_legacy_network(client)
    return networks


def is_docker_available() -> bool:
    try:
        client = _get_client()
        client.ping()
        return True
    except Exception:
        return False
