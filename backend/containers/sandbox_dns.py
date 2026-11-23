"""Names inside a gVisor sandbox (tenant mode, runtime runsc).

On its own networks Docker answers names with a server of its own at 127.0.0.11, which a gVisor sandbox cannot
reach (its network stack is its own; gVisor issue 7469). Without this nothing resolves in there: not public names
(discord.com), not the databases and apps it is linked to. So a sandboxed container gets
  * a resolv.conf of its own with this machine's real name servers (what the gVisor FAQ recommends), mounted over
    /etc/resolv.conf; an app that may not connect to anything gets none, as before;
  * the names of the databases and apps it is linked to on this machine, with their addresses, in /etc/hosts.
Its URLs stay the same. A linked database or app that is not running when the container starts is not in /etc/hosts;
the container finds it after its next start.

Blocking: call from a thread.
"""
import ipaddress
import logging
import os
import re
import tempfile

from containers import client as containers_client, naming
from dbs import lifecycle as dbs_lifecycle, proxy as db_proxy

log = logging.getLogger("cloudbase.docker")

RESOLV_FILE = os.path.expanduser("~/.cloudbase/sandbox-resolv.conf")
# systemd-resolved keeps the real upstream servers here; /etc/resolv.conf then only has its local stub (127.0.0.53)
SOURCES = ("/run/systemd/resolve/resolv.conf", "/etc/resolv.conf")
FALLBACK = ("1.1.1.1", "8.8.8.8")


def sandboxed(docker_options: dict) -> bool:
    return bool(((docker_options or {}).get("hardening") or {}).get("runtime"))


def name_servers(sources=SOURCES) -> list[str]:
    """This machine's real name servers (IPv4, not on the machine itself), or public ones when it has none."""
    for path in sources:
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        found = []
        for line in lines:
            words = line.split()
            if len(words) >= 2 and words[0] == "nameserver":
                try:
                    ip = ipaddress.ip_address(words[1])
                except ValueError:
                    continue
                if ip.version == 4 and not ip.is_loopback and str(ip) not in found:
                    found.append(str(ip))
        if found:
            return found[:3]
    return list(FALLBACK)


def resolv_conf() -> str:
    """The file to mount (written again when the name servers changed)."""
    text = "# Written by Cloudbase for containers in the gVisor sandbox (containers/sandbox_dns.py)\n"
    text += "".join(f"nameserver {ip}\n" for ip in name_servers())
    try:
        with open(RESOLV_FILE, encoding="utf-8") as f:
            if f.read() == text:
                return RESOLV_FILE
    except OSError:
        pass
    os.makedirs(os.path.dirname(RESOLV_FILE), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(RESOLV_FILE))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o644)                        # read by the container's processes
    os.replace(tmp, RESOLV_FILE)
    return RESOLV_FILE


def _address_on(container, network: str) -> str | None:
    networks = (container.attrs.get("NetworkSettings") or {}).get("Networks") or {}
    return (networks.get(network) or {}).get("IPAddress") or None


def linked_hosts(docker, docker_options: dict) -> dict[str, str]:
    """{name: address} of the databases and apps the container is linked to and that run on this machine now."""
    hosts: dict[str, str] = {}
    me = db_proxy.local_node_id()
    for database_id in db_proxy.local_database_ids(docker_options.get("linked_databases") or []):
        try:
            ip, _port = dbs_lifecycle.container_address(int(database_id))
            hosts[naming.database_container_name(int(database_id))] = ip
        except Exception as exc:
            log.info("[sandbox] database %s is not running yet, its name is left out: %s", database_id, exc)
    for entry in docker_options.get("linked_apps") or []:
        route, _target = containers_client.link_route(entry, me)
        found = re.fullmatch(r"app-(\d+)", entry.get("alias") or "")
        if route != "here" or not found:
            continue
        for container in docker.containers.list(filters={"name": f"cloudbase-app-{found.group(1)}-instance-"}):
            ip = _address_on(container, entry.get("network") or "")
            if ip:
                hosts[entry["alias"]] = ip
                break
    return hosts


def apply(docker, run_kwargs: dict, docker_options: dict) -> None:
    """Give a sandboxed container its names. Nothing for one that is not sandboxed."""
    if not sandboxed(docker_options):
        return
    if containers_client.network_access(docker_options) != "none":
        volumes = dict(run_kwargs.get("volumes") or {})
        volumes[resolv_conf()] = {"bind": "/etc/resolv.conf", "mode": "ro"}
        run_kwargs["volumes"] = volumes
    hosts = linked_hosts(docker, docker_options)
    if hosts:
        run_kwargs["extra_hosts"] = {**(run_kwargs.get("extra_hosts") or {}), **hosts}
