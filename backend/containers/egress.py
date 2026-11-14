"""What the apps on this machine that may not reach everything are still allowed to connect to.

An app with internet-only access (or none) can connect to the apps and databases it is connected to on other nodes.
Those destinations are in the specs of its instances; this module collects them and hands them to the root helper,
which keeps the firewall rule (scripts/cloudbase-wireguard). The reconciler calls it whenever the list of this machine
changes, so a new connection is open before the instance that needs it starts. A cron job runs outside that list:
it opens its own destinations while it runs.
"""
import logging
import threading

from containers import client as containers_client
from dbs import proxy as db_proxy
from mesh import helper

log = logging.getLogger("cloudbase.docker")

RESTRICTED = ("internet", "none")

_lock = threading.Lock()
_instances: set[tuple[str, int]] = set()          # from the list of this machine
_jobs: dict[str, set[tuple[str, int]]] = {}       # running cron jobs: container name -> destinations
_applied: list[dict] | None = None


def endpoint(url: str) -> tuple[str, int] | None:
    """(host, port) of a database URL such as postgresql://user:password@10.77.0.3:30007/name."""
    try:
        authority = url.split("://", 1)[1].rsplit("@", 1)[-1].split("/", 1)[0].split("?", 1)[0]
        host, _, port = authority.rpartition(":")
        return (host, int(port)) if host and port else None
    except (IndexError, ValueError):
        return None


def targets_of(options: dict) -> set[tuple[str, int]]:
    """The destinations on other nodes that an app with these docker options connects to; none for an open app."""
    if containers_client.network_access(options) not in RESTRICTED:
        return set()
    me = db_proxy.local_node_id()
    found: set[tuple[str, int]] = set()
    for linked in options.get("linked_apps") or []:
        route, target = containers_client.link_route(linked, me)
        if route == "remote":
            found.add((target["address"], int(target["port"])))
    for link in options.get("linked_databases") or []:
        if link.get("remote_url") and not db_proxy.is_local(link, me):
            reached = endpoint(link["remote_url"])
            if reached:
                found.add(reached)
    return found


def allowed_targets(entries: dict[int, dict]) -> list[dict]:
    """[{"ip", "port"}]: where the instances on this machine connect to on other nodes."""
    found: set[tuple[str, int]] = set()
    for entry in entries.values():
        if entry.get("desired") == "running":
            found |= targets_of((entry.get("spec") or {}).get("docker_options") or {})
    return [{"ip": ip, "port": port} for ip, port in sorted(found)]


def _push() -> None:
    """Send the union of what instances and running jobs need, when it differs from what was sent last."""
    global _applied
    union = set(_instances).union(*_jobs.values()) if _jobs else set(_instances)
    allowed = [{"ip": ip, "port": port} for ip, port in sorted(union)]
    if allowed == _applied:
        return
    helper.set_egress(allowed)
    _applied = allowed
    log.info("[egress] %d destination(s) open for apps that may not reach everything", len(allowed))


def apply(entries: dict[int, dict]) -> None:
    """Blocking: call from a thread. Nothing is sent when the list is what was sent last."""
    global _instances
    with _lock:
        _instances = {(t["ip"], t["port"]) for t in allowed_targets(entries)}
        _push()


def open_for_job(name: str, options: dict) -> None:
    """A cron job is about to start: open the destinations it needs, until close_job."""
    targets = targets_of(options)
    if not targets:
        return
    with _lock:
        _jobs[name] = targets
        _push()


def close_job(name: str) -> None:
    with _lock:
        if _jobs.pop(name, None) is not None:
            _push()
