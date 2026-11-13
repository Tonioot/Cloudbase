import logging
import re
import threading
from dbs import proxy as db_proxy
from containers import naming
from mesh import helper

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


_firewall_done = False


def _isolate_from_host() -> None:
    """Ask the root helper, once per run, to keep containers from starting connections to this machine itself. Never
    stops an app from starting: an old helper (not updated yet) only means the apps are less shut off."""
    global _firewall_done
    if _firewall_done:
        return
    try:
        helper.isolate_apps()
        _firewall_done = True
    except Exception as exc:
        log.warning("[docker] Apps can still reach this machine's own ports: %s (run: cloudbase update)", exc)


def ensure_apps_network() -> None:
    ensure_network(naming.APPS_NETWORK, "cbapps0", isolated=True)
    _isolate_from_host()


def ensure_database_network(database_id: int) -> str:
    name = naming.database_network_name(database_id)
    ensure_network(name, f"cbdb{database_id}")
    return name


def ensure_link_network(network: str) -> str:
    """The network of an app link: cloudbase-link-<id>."""
    ensure_network(network, "cbl" + network.rsplit("-", 1)[1])
    return network


def attach(client, network: str, container, aliases: list[str] | None = None) -> None:
    """Connect a container to a network, unless it already is. `aliases`: the names it answers to there."""
    container.reload()
    if network not in ((container.attrs.get("NetworkSettings") or {}).get("Networks") or {}):
        client.networks.get(network).connect(container, aliases=aliases or None)


def _join_database_networks(client, linked: list[dict]) -> list[tuple[str, list[str]]]:
    """The networks of the linked databases that run here, with each database container on its own network."""
    import docker  # type: ignore
    joined = []
    for database_id in db_proxy.local_database_ids(linked):
        name = ensure_database_network(database_id)
        try:      # a database container made before this network existed is moved onto it here
            attach(client, name, client.containers.get(naming.database_container_name(database_id)))
        except docker.errors.NotFound:
            pass  # not started yet: it is created on its own network
        joined.append((name, []))
    return joined


def _link_apps(docker_options: dict) -> tuple[dict, list[tuple[str, list[str]]]]:
    """The links to other apps: the address of each as an env var, and the networks it is reached on.

    A target with an instance on this node is reached over a network the two share, by its alias: private, and it does
    not leave the machine. One that runs only on other nodes is reached at the address its node publishes it on
    (over the node network). With no instance running yet the alias is used, which works as soon as one starts here."""
    me = db_proxy.local_node_id()
    env, joined = {}, []
    for entry in docker_options.get("linked_apps") or []:
        targets = entry.get("targets") or []
        remote = sorted((t for t in targets if t.get("address")), key=lambda t: t["node_id"])
        here = me is None or any(t["node_id"] == me for t in targets) or not remote
        if here:
            env[entry["env_var"]] = f"http://{entry['alias']}:{entry['port']}"
            joined.append((ensure_link_network(entry["network"]), []))
        else:
            env[entry["env_var"]] = f"http://{remote[0]['address']}:{remote[0]['port']}"
    for entry in docker_options.get("exposed_to") or []:
        joined.append((ensure_link_network(entry["network"]), [entry["alias"]]))
    return env, joined


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


def start_container(client, img: str, run_kwargs: dict, extra_networks: list[tuple[str, list[str]]]):
    """Run a container on its network and on the extra ones (name, aliases), which Docker can only add once it exists."""
    if not extra_networks:
        return client.containers.run(img, **run_kwargs)
    container = client.containers.create(img, **{k: v for k, v in run_kwargs.items() if k != "detach"})
    try:
        for name, aliases in extra_networks:
            attach(client, name, container, aliases)
        container.start()
    except Exception:
        container.remove(force=True)
        raise
    return container


def _host_port(external_port: int, docker_options: dict):
    """Docker port binding: on localhost only when docker_options asks for it."""
    bind_host = (docker_options or {}).get("bind_host")
    return (bind_host, external_port) if bind_host else external_port


def _apply_linked_databases(client, run_kwargs: dict, docker_options: dict) -> list[tuple[str, list[str]]]:
    """Put the container on the apps network, where it cannot reach other apps, and add what its links give it: the
    URLs of linked databases (direct on the database's node, over the node network elsewhere) and of linked apps.
    A link wins when the app has an env var of the same name (an imported .env must not silently cut the connection).
    Returns the extra networks to join (name, aliases): those of the databases and apps it is linked to."""
    ensure_apps_network()
    run_kwargs["network"] = naming.APPS_NETWORK
    linked = docker_options.get("linked_databases") or []
    linked_env = db_proxy.resolve_links(linked) if linked else {}
    networks = _join_database_networks(client, linked) if linked else []
    app_env, app_networks = _link_apps(docker_options)
    if linked_env or app_env:
        run_kwargs["environment"] = {**(run_kwargs.get("environment") or {}), **linked_env, **app_env}
    retire_legacy_network(client)
    return networks + app_networks


def is_docker_available() -> bool:
    try:
        client = _get_client()
        client.ping()
        return True
    except Exception:
        return False
