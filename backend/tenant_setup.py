"""`cloudbase tenant`: turn tenant mode (core/tenant.py) on or off on the primary, asking what you want.

    python tenant_setup.py [on|off|status] [--yes]

on (the default) asks a few questions, shows what it will write and writes the `tenant:` section of config.yaml.
--yes takes the current values (or the defaults) without asking. start.sh restarts Cloudbase afterwards.
Exit status 0 when something was written (start.sh then restarts), 3 when nothing changed, 1 on an error.
"""
import asyncio
import os
import sys

from sqlalchemy import select

from core import config as _cfg
from core.database import AsyncSessionLocal
from core.models import Application, Database, Instance, Node

UNCHANGED = 3

BOLD, DIM, YELLOW, GREEN, RESET = "\033[1m", "\033[2m", "\033[33m", "\033[32m", "\033[0m"


def is_node() -> bool:
    return os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_state.json"))


async def on_the_primary() -> dict:
    """What runs on the primary now: tenant mode places nothing new there and won't restart it."""
    async with AsyncSessionLocal() as db:
        local = (await db.execute(select(Node.id).where(Node.is_local == True))).scalar_one_or_none()   # noqa: E712
        if local is None:
            return {"apps": [], "cron": [], "databases": [], "nodes": []}
        apps = (await db.execute(select(Application.name).join(Instance, Instance.app_id == Application.id)
                                 .where(Instance.node_id == local).distinct())).scalars().all()
        cron = (await db.execute(select(Application.name).where(
            Application.cron_schedule.is_not(None), Application.node_id.is_(None)))).scalars().all()
        dbs = (await db.execute(select(Database.name).where(Database.node_id == local))).scalars().all()
        nodes = (await db.execute(select(Node.name, Node.status).where(Node.is_local == False,   # noqa: E712
                                                                       Node.enabled == True))).all()  # noqa: E712
    return {"apps": sorted(apps), "cron": sorted(cron), "databases": sorted(dbs),
            "nodes": [(name, status) for name, status in nodes]}


def ask(question: str, default: str) -> str:
    answer = input(f"{question} {DIM}[{default}]{RESET} ").strip()
    return answer or default


def ask_yes(question: str, default: bool) -> bool:
    while True:
        answer = input(f"{question} {DIM}[{'Y/n' if default else 'y/N'}]{RESET} ").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes", "j", "ja"):
            return True
        if answer in ("n", "no", "nee"):
            return False


def ask_number(question: str, default, kind=int, low=None, high=None):
    while True:
        raw = ask(question, str(default))
        try:
            value = kind(raw)
        except ValueError:
            print(f"  {YELLOW}That is not a number.{RESET}")
            continue
        if (low is not None and value < low) or (high is not None and value > high):
            print(f"  {YELLOW}Between {low} and {high}.{RESET}")
            continue
        return value


def describe(settings: dict) -> list[str]:
    return [
        f"Sandbox:                 {'gVisor (runsc)' if settings.get('runtime') == 'runsc' else (settings.get('runtime') or 'none (Docker’s own runtime)')}"
        + (", also for databases" if settings.get("runtime") and settings.get("sandbox_databases") else ""),
        f"Default CPU limit:       {settings['default_cpu_limit']} core(s) for apps and databases without their own",
        f"Default memory limit:    {settings['default_memory_limit_mb']} MB",
        f"Processes per container: {settings['pids_limit']}",
        f"Build time limit:        {settings['build_timeout_seconds'] // 60} minutes",
        f"Disk per container:      {str(settings['container_disk_mb']) + ' MB' if settings['container_disk_mb'] else 'off'}",
    ]


def print_status(settings: dict, world: dict) -> None:
    state = f"{GREEN}on{RESET}" if settings.get("enabled") else "off"
    print(f"{BOLD}Tenant mode is {state}{RESET}")
    if settings.get("enabled"):
        for line in describe(settings):
            print("  " + line)
    print_world(world)


def print_world(world: dict) -> None:
    nodes = world.get("nodes") or []
    online = [n for n, s in nodes if s == "online"]
    print(f"\n{BOLD}Nodes{RESET}: {len(online)} online of {len(nodes)}"
          + (f" ({', '.join(online)})" if online else ""))
    if not online:
        print(f"  {YELLOW}With tenant mode nothing runs on the primary: without a node nothing new can start.{RESET}")
    here = [("apps", world["apps"]), ("cron jobs", world["cron"]), ("databases", world["databases"])]
    if any(names for _, names in here):
        print(f"\n{YELLOW}{BOLD}Running on the primary now{RESET}")
        for label, names in here:
            if names:
                print(f"  {label}: {', '.join(names)}")
        print(f"  {DIM}In tenant mode these keep running as they are, but they can't be restarted, deployed or")
        print("  scaled there. Move them to a node first (apps: scale onto a node and remove the primary's")
        print(f"  instance; databases: Settings → Move).{RESET}")


def ask_settings(current: dict) -> dict:
    s = dict(current)
    print(f"\n{BOLD}1. Sandbox{RESET}")
    print("  gVisor gives every container its own kernel in user space: a bug in the Linux kernel is then no way")
    print("  out of a container. It costs some speed on file and network heavy apps. It must be installed on")
    print("  every node (cloudbase gvisor); a container that should be sandboxed never starts without it.")
    s["runtime"] = "runsc" if ask_yes("  Use gVisor?", default=current.get("runtime") == "runsc" or not current.get("enabled")) else ""
    s["sandbox_databases"] = False
    if s["runtime"]:
        print(f"  {DIM}Databases are slowed down most by it, and their users can't run programs on the server (a")
        print(f"  PostgreSQL user is the owner of its database, not a superuser). Off is the usual choice.{RESET}")
        s["sandbox_databases"] = ask_yes("  gVisor for databases too?", default=bool(current.get("sandbox_databases")))

    print(f"\n{BOLD}2. Limits for apps and databases that don't set their own{RESET}")
    s["default_cpu_limit"] = ask_number("  CPU cores", float(current["default_cpu_limit"]), float, 0.1, 64)
    s["default_memory_limit_mb"] = ask_number("  Memory (MB)", int(current["default_memory_limit_mb"]), int, 64, 1024 * 1024)

    print(f"\n{BOLD}3. Per container{RESET}")
    s["pids_limit"] = ask_number("  Most processes (a fork bomb stops there)", int(current["pids_limit"]), int, 32, 65536)
    s["build_timeout_seconds"] = 60 * ask_number("  Most minutes a build may take", int(current["build_timeout_seconds"]) // 60, int, 1, 600)
    print(f"  {DIM}A disk limit per container needs Docker's storage on xfs with pquota on every node; otherwise 0.{RESET}")
    s["container_disk_mb"] = ask_number("  Disk per container in MB (0 = off)", int(current["container_disk_mb"]), int, 0, 1024 * 1024)
    return s


def write(settings: dict) -> None:
    keys = ("enabled", "default_cpu_limit", "default_memory_limit_mb", "pids_limit", "runtime", "sandbox_databases",
            "build_timeout_seconds", "container_disk_mb")
    _cfg.save_config_sections({"tenant": {k: settings[k] for k in keys}})


def turn_on(current: dict, world: dict, yes: bool) -> int:
    print(f"{BOLD}Tenant mode{RESET}: for apps and databases of people you don't know. Apps can't reach the local")
    print("network, other apps or the servers; containers get limits and lose risky rights; builds and apps never")
    print("run on the primary; mail (port 25) out is blocked; nodes only forward app and database ports.")
    print_world(world)
    if (world["apps"] or world["cron"] or world["databases"]) and not yes:
        if not ask_yes("\nTurn tenant mode on anyway?", default=False):
            return UNCHANGED

    settings = {**current, "enabled": True}
    if not yes:
        settings = {**ask_settings(current), "enabled": True}
    elif not current.get("enabled"):
        settings["runtime"] = current.get("runtime") or ""

    print(f"\n{BOLD}This goes into config.yaml{RESET}")
    for line in describe(settings):
        print("  " + line)
    if not yes and not ask_yes("\nTurn tenant mode on with these settings?", default=True):
        return UNCHANGED
    if settings == current:
        print("Nothing changed.")
        return UNCHANGED
    write(settings)
    print(f"\n{GREEN}Written.{RESET} Next:")
    step = 1
    if settings.get("runtime") == "runsc":
        print(f"  {step}. On every node: cloudbase gvisor")
        step += 1
    print(f"  {step}. On every node, once it has picked up its list: cloudbase tenant-check")
    return 0


def turn_off(current: dict, yes: bool) -> int:
    if not current.get("enabled"):
        print("Tenant mode is already off.")
        return UNCHANGED
    print("Without tenant mode apps may reach the local network again (if set to), run on the primary, build there,")
    print("and containers lose their limits and the sandbox at their next start.")
    if not yes and not ask_yes("Turn tenant mode off?", default=False):
        return UNCHANGED
    write({**current, "enabled": False})
    print(f"{GREEN}Written.{RESET}")
    return 0


def main(argv: list[str]) -> int:
    yes = "--yes" in argv or "-y" in argv
    words = [a for a in argv if not a.startswith("-")]
    action = words[0] if words else "on"
    if action not in ("on", "off", "status"):
        print("usage: cloudbase tenant [on|off|status] [--yes]", file=sys.stderr)
        return 1
    if is_node():
        print("This is a node: tenant mode is set on the primary, and the nodes take it from there.", file=sys.stderr)
        return 1
    current = _cfg.get_tenant()
    world = asyncio.run(on_the_primary())
    if action == "status":
        print_status(current, world)
        return UNCHANGED
    if not yes and not sys.stdin.isatty():
        print("Not a terminal: add --yes to take the current values without asking.", file=sys.stderr)
        return 1
    try:
        return turn_on(current, world, yes) if action == "on" else turn_off(current, yes)
    except (KeyboardInterrupt, EOFError):
        print("\nNothing changed.")
        return UNCHANGED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
