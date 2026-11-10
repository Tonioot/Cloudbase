import logging
import threading
from dbs import proxy as db_proxy

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


def ensure_network(name: str) -> None:
    """Create the user-defined bridge network if it does not exist yet."""
    import docker  # type: ignore
    client = _get_client()
    with _network_lock:
        try:
            client.networks.get(name)
        except docker.errors.NotFound:
            # A fixed bridge name makes firewall rules for the database proxies simple.
            client.networks.create(
                name, driver="bridge", labels={"cloudbase.managed": "true"},
                options={"com.docker.network.bridge.name": f"{name[:10]}0"},
            )
            log.info("[docker] Created network %s", name)


def _host_port(external_port: int, docker_options: dict):
    """Docker port binding: on localhost only when docker_options asks for it."""
    bind_host = (docker_options or {}).get("bind_host")
    return (bind_host, external_port) if bind_host else external_port


def _apply_linked_databases(run_kwargs: dict, docker_options: dict) -> None:
    """Attach the container to the database network and add the URLs of linked
    databases: direct on the database's node, over the node network elsewhere.
    The app's own env vars win when a name is set in both places."""
    linked = docker_options.get("linked_databases") or []
    if linked:
        linked_env = db_proxy.resolve_links(linked)
        run_kwargs["environment"] = {**linked_env, **(run_kwargs.get("environment") or {})}
    network = docker_options.get("network")
    if network:
        ensure_network(network)
        run_kwargs["network"] = network


def is_docker_available() -> bool:
    try:
        client = _get_client()
        client.ping()
        return True
    except Exception:
        return False
