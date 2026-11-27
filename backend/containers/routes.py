"""How a container reaches the apps and databases of its project: at an address that never changes.

An app gets `http://app-<id>:<port>` (a database `...@db-<id>:<port>/...`). The name points at this machine, on the
project's own network (/etc/hosts, containers/client.py), and a firewall rule sends what comes in there on that port to
wherever the target runs now: its container here, or its published port on another node. No program sits in between;
the kernel forwards it. A deploy, a restart or a move only changes the rule, so nothing that connects has to restart.
Only containers on the project's network match the rules.

The primary says where the targets are (the "routes" of the node's list, apps/app_links.py). This module turns them into
rules for the networks and containers this machine has, and the root helper puts them in the firewall
(scripts/cloudbase-wireguard, which also keeps them for the next boot).

Blocking: call from a thread.
"""
import logging
import threading
import time

from containers import client as containers_client
from mesh import helper

log = logging.getLogger("cloudbase.docker")

REFRESH_SECONDS = 60      # sent again this often even when nothing changed: something else may have cleared the rules

_lock = threading.Lock()
_wanted: tuple | None = None             # (routes, this node's id); None until the primary said: nothing is changed
_applied: list[dict] | None = None       # what the firewall was last given
_applied_at = 0.0
_failed: tuple | None = None              # (rules, when) of a try that failed: not repeated at every pass
MAX_TARGETS = 100                         # what the helper accepts for one address


def set_wanted(routes: list[dict], node_id: int) -> None:
    """Never waits (one assignment): called on the event loop while a thread may be applying."""
    global _wanted
    _wanted = (list(routes), node_id)


def _address(container, network: str) -> str | None:
    """The container's address, the one on `network` when it is on it."""
    if container.status != "running":
        return None
    networks = (container.attrs.get("NetworkSettings") or {}).get("Networks") or {}
    ordered = [networks.get(network) or {}] + list(networks.values())
    return next((n["IPAddress"] for n in ordered if n.get("IPAddress")), None)


def rules(docker, wanted: list[dict], me: int) -> list[dict]:
    """[{"bridge", "gateway", "port", "targets": [{"ip", "port"}]}] for this machine. A route of a network that is not
    here is left out (nothing here is on it), and so is a target that is not running."""
    import docker as docker_sdk  # type: ignore
    networks: dict[str, tuple | None] = {}
    containers: dict[str, object] = {}

    def network(name):
        if name not in networks:
            try:
                attrs = docker.networks.get(name).attrs
                bridge = (attrs.get("Options") or {}).get("com.docker.network.bridge.name")
                gateway = next((c["Gateway"] for c in (attrs.get("IPAM") or {}).get("Config") or [] if c.get("Gateway")), None)
                networks[name] = (bridge, gateway.split("/")[0]) if bridge and gateway else None
            except docker_sdk.errors.NotFound:
                networks[name] = None
        return networks[name]

    def container(name):
        if name not in containers:
            try:
                containers[name] = docker.containers.get(name)
            except docker_sdk.errors.NotFound:
                containers[name] = None
        return containers[name]

    out, seen = [], set()
    for route in wanted:
        found = network(route["network"])
        if found is None:
            continue
        targets = []
        for target in route.get("targets") or []:
            if target.get("node_id") == me:
                here = container(target["container"])
                ip = _address(here, route["network"]) if here is not None else None
                if ip:
                    label = (here.labels or {}).get("cloudbase.internal_port")
                    targets.append({"ip": ip, "port": int(label or target["internal_port"])})
            elif target.get("address") and target.get("port"):
                targets.append({"ip": target["address"], "port": int(target["port"])})
        address = (found[0], int(route["port"]))
        if targets and address not in seen:          # two ids that share a port: the first, not a refused list
            seen.add(address)
            out.append({"bridge": found[0], "gateway": found[1], "port": address[1], "targets": targets[:MAX_TARGETS]})
    return out


def apply() -> None:
    """Make the firewall match what the primary wants and what runs here now. Never raises: a machine whose helper is
    old or missing keeps running its apps, which then can't reach each other (said in the log)."""
    global _applied, _applied_at, _failed
    with _lock:
        if _wanted is None:
            return
        wanted = None
        try:
            wanted = rules(containers_client._get_client(), *_wanted)
            now = time.monotonic()
            if wanted == _applied and now - _applied_at < REFRESH_SECONDS:
                return
            if _failed and _failed[0] == wanted and now - _failed[1] < REFRESH_SECONDS:
                return
            helper.set_routes(wanted)
            if wanted != _applied:
                log.info("[routes] %d address(es) of projects forwarded", len(wanted))
            _applied, _applied_at, _failed = wanted, time.monotonic(), None
        except Exception as exc:
            _failed, _applied = (wanted, time.monotonic()), None
            log.warning("[routes] could not set the rules, so apps may not reach each other: %s (run: cloudbase update)", exc)
