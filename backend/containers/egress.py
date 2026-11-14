"""What the apps on this machine that may not reach the local network are still allowed to connect to.

Such an app can use the internet, and connect to the apps and databases it is connected to on other nodes. Those
destinations are in the specs of its instances; this module collects them and hands them to the root helper, which
keeps the firewall rule (scripts/cloudbase-wireguard). Called by the reconciler whenever the list of this machine
changes, so a new connection is open before the instance that needs it starts.
"""
import logging

from containers import client as containers_client
from dbs import proxy as db_proxy
from mesh import helper

log = logging.getLogger("cloudbase.docker")

_applied: list[dict] | None = None


def endpoint(url: str) -> tuple[str, int] | None:
    """(host, port) of a database URL such as postgresql://user:password@10.77.0.3:30007/name."""
    try:
        authority = url.split("://", 1)[1].rsplit("@", 1)[-1].split("/", 1)[0].split("?", 1)[0]
        host, _, port = authority.rpartition(":")
        return (host, int(port)) if host and port else None
    except (IndexError, ValueError):
        return None


def allowed_targets(entries: dict[int, dict]) -> list[dict]:
    """[{"ip", "port"}]: where the instances on this machine that may not reach the local network connect to elsewhere."""
    me = db_proxy.local_node_id()
    found: set[tuple[str, int]] = set()
    for entry in entries.values():
        if entry.get("desired") != "running":
            continue
        options = (entry.get("spec") or {}).get("docker_options") or {}
        if not options.get("block_local_network"):
            continue                                  # an app that may reach the local network needs no list
        for linked in options.get("linked_apps") or []:
            route, target = containers_client.link_route(linked, me)
            if route == "remote":
                found.add((target["address"], int(target["port"])))
        for link in options.get("linked_databases") or []:
            if link.get("remote_url") and not db_proxy.is_local(link, me):
                reached = endpoint(link["remote_url"])
                if reached:
                    found.add(reached)
    return [{"ip": ip, "port": port} for ip, port in sorted(found)]


def apply(entries: dict[int, dict]) -> None:
    """Blocking: call from a thread. Nothing happens when the list is what was applied last."""
    global _applied
    allowed = allowed_targets(entries)
    if allowed == _applied:
        return
    helper.set_egress(allowed)
    _applied = allowed
    log.info("[egress] %d destination(s) open for apps that may not reach the local network", len(allowed))
