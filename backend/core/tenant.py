"""Tenant mode: the apps and databases belong to people you don't know (a hosting service built on Cloudbase).

The primary decides (config.yaml, section `tenant`) and writes what follows from it into the specs it hands out
(`hardening` in the docker options); a node applies what the spec says (containers/hardening.py) and needs no
setting of its own. With tenant mode off nothing changes.

What it means:
  * apps reach the internet or nothing, never the local network (network access "open" is refused);
  * every container has CPU, memory and process limits, and only the capabilities ordinary software needs;
  * builds never use the host network and stop after a while;
  * no app, cron job or database runs on the primary, which holds the panel, its database and every secret;
  * the firewall blocks mail (port 25) out, and the hub only forwards app and database ports between nodes.
"""
from typing import Optional

from core import config as _cfg

# Docker gives a container 14 capabilities by default. These are what ordinary software (and the entrypoints of the
# official database images) uses; left out are e.g. NET_RAW (forged packets on the bridge), MKNOD, SYS_CHROOT,
# SETFCAP, SETPCAP, AUDIT_WRITE, FSETID.
CAPABILITIES = ["CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID", "NET_BIND_SERVICE", "KILL"]


def enabled() -> bool:
    return bool(_cfg.get_tenant().get("enabled"))


def settings() -> dict:
    return _cfg.get_tenant()


def hardening(database: bool = False) -> Optional[dict]:
    """What a container must get, for the spec; None with tenant mode off. A database gets the sandbox only when
    asked for (sandbox_databases): its users can't run programs (engines.PG_ROLES_SCRIPT), and it is the part that
    the sandbox slows down most."""
    if not enabled():
        return None
    s = settings()
    runtime = str(s.get("runtime") or "")
    if database and not s.get("sandbox_databases"):
        runtime = ""
    return {
        "cap_add": list(CAPABILITIES),
        "pids_limit": int(s.get("pids_limit") or 512),
        "runtime": runtime,
        "disk_mb": int(s.get("container_disk_mb") if s.get("container_disk_mb") is not None else 1024),
        "build_timeout_seconds": int(s.get("build_timeout_seconds") or 1800),
        "isolated_build": True,
    }


def limits(cpu_limit, memory_limit_mb) -> tuple:
    """(cpu, memory MB): what was set, or in tenant mode the defaults for what was not."""
    if not enabled():
        return cpu_limit, memory_limit_mb
    s = settings()
    return (cpu_limit or float(s.get("default_cpu_limit") or 1.0),
            memory_limit_mb or int(s.get("default_memory_limit_mb") or 512))


def network_access(value: str) -> str:
    """In tenant mode an app reaches the internet or nothing: an old "open" becomes "internet"."""
    return "internet" if enabled() and value == "open" else value


def may_run_on(node) -> bool:
    """Whether apps, cron jobs and databases may be placed on this node."""
    return not (enabled() and node is not None and node.is_local)


REFUSED_ON_PRIMARY = "Tenant mode: nothing runs on the primary. Pick a node (or add one)."
