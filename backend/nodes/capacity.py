"""Reservations: what every node may give out, and what is promised on it.

A node gives out its cores and memory minus a reserve for the system (Docker, gVisor, the agent):

    memory      never more than there is: the memory limits of everything on a node add up to at most what it gives out
    CPU         a little overcommit: the CPU limits add up to at most cpu_ratio times the cores it gives out
    disk        the disks of its databases add up to at most its disk minus the reserve

What is promised on a node: every instance that should run, every cron job placed there and every database that runs
(and the disk of every database there, running or not), each with its limits. Something without a limit counts with the
default of tenant mode, or 1 core and 512 MB.

Something new only goes to a node where it fits, and is refused with the reason where it fits nowhere. What already
runs is left alone, also on a node that is too full (after the settings changed): nothing is stopped or moved, but
nothing new is added there, and a restart that does not ask for more is always allowed.

With deploy_room, a new placement leaves room on its node for one more instance of the biggest app there: a rolling
deploy starts the new version next to the old one, and that one instance may use the room (surge()).

The primary in tenant mode runs nothing and gives out nothing. mode "auto" turns reservations on in tenant mode only:
a Cloudbase at home keeps placing as it always did.
"""
import contextlib
import contextvars
import json
from dataclasses import dataclass
from typing import Iterable, Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core import config as _cfg, tenant
from core.env_crypto import decrypt_text
from core.models import Application, Database, Instance, Node

_surge: contextvars.ContextVar[bool] = contextvars.ContextVar("capacity_surge", default=False)

LIMITS = {
    "reserve_cpu": (0.0, 64.0), "reserve_memory_mb": (0, 262144), "reserve_disk_gb": (0, 100000), "cpu_ratio": (1.0, 16.0),
}


def settings() -> dict:
    s = _cfg.get_capacity()
    out = {"mode": s.get("mode") if s.get("mode") in ("on", "off", "auto") else "auto", "deploy_room": bool(s.get("deploy_room"))}
    for key, (lo, hi) in LIMITS.items():
        try:
            out[key] = min(hi, max(lo, float(s.get(key))))
        except (TypeError, ValueError):
            out[key] = float(_cfg._DEFAULTS["capacity"][key])
    out["reserve_memory_mb"], out["reserve_disk_gb"] = int(out["reserve_memory_mb"]), int(out["reserve_disk_gb"])
    return out


def clean_settings(body: dict) -> dict:
    """The settings a caller may change, within their limits. Raises HTTPException(400) on a value out of range."""
    out = {}
    if "mode" in body:
        if body["mode"] not in ("on", "off", "auto"):
            raise HTTPException(400, "mode must be on, off or auto")
        out["mode"] = body["mode"]
    if "deploy_room" in body:
        out["deploy_room"] = bool(body["deploy_room"])
    for key, (lo, hi) in LIMITS.items():
        if key in body and body[key] is not None:
            try:
                value = float(body[key])
            except (TypeError, ValueError):
                raise HTTPException(400, f"{key} must be a number")
            if not lo <= value <= hi:
                raise HTTPException(400, f"{key} must be between {lo:g} and {hi:g}")
            out[key] = int(value) if key in ("reserve_memory_mb", "reserve_disk_gb") else value
    return out


def enabled() -> bool:
    mode = settings()["mode"]
    return mode == "on" or (mode == "auto" and tenant.enabled())


@contextlib.contextmanager
def surge():
    """A deploy starts the new version next to the old one: that instance may use the room kept for deploys."""
    token = _surge.set(True)
    try:
        yield
    finally:
        _surge.reset(token)


# ── What things need ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Need:
    cpu: float = 0.0
    memory_mb: int = 0
    disk_mb: int = 0

    def __add__(self, other: "Need") -> "Need":
        return Need(self.cpu + other.cpu, self.memory_mb + other.memory_mb, self.disk_mb + other.disk_mb)

    def __sub__(self, other: "Need") -> "Need":
        return Need(self.cpu - other.cpu, self.memory_mb - other.memory_mb, self.disk_mb - other.disk_mb)

    def at_most(self, other: "Need") -> bool:
        return self.cpu <= other.cpu + 1e-9 and self.memory_mb <= other.memory_mb and self.disk_mb <= other.disk_mb

    def as_dict(self) -> dict:
        return {"cpu": round(self.cpu, 3), "memory_mb": int(self.memory_mb), "disk_mb": int(self.disk_mb)}


def _limits(cpu, memory_mb) -> tuple[float, int]:
    cpu, memory_mb = tenant.limits(cpu, memory_mb)
    return float(cpu or 1.0), int(memory_mb or 512)


def need_of(cpu=None, memory_mb=None, disk_mb=None) -> Need:
    cpu, memory_mb = _limits(cpu, memory_mb)
    return Need(cpu, memory_mb, int(disk_mb or 0))


def instance_need(instance: Instance, app: Application) -> Need:
    """An instance's own limits win over its app's (as in its spec)."""
    return need_of(instance.docker_cpu_limit or app.docker_cpu_limit, instance.docker_memory_limit_mb or app.docker_memory_limit_mb)


def spec_need(instance: Instance) -> Optional[Need]:
    """What the instance's current spec asks for: what its container runs with (None without a spec)."""
    if not instance.spec_json:
        return None
    try:
        return payload_need(json.loads(decrypt_text(instance.spec_json)))
    except Exception:
        return None


def running_need(instance: Instance, app: Application) -> Need:
    """What is promised for an instance that should run: its spec's limits, else its own and its app's."""
    return spec_need(instance) or instance_need(instance, app)


def app_need(app: Application) -> Need:
    return need_of(app.docker_cpu_limit, app.docker_memory_limit_mb)


def payload_need(payload: dict) -> Need:
    return need_of(payload.get("docker_cpu_limit"), payload.get("docker_memory_limit_mb"))


def database_need(database: Database, *, running: Optional[bool] = None) -> Need:
    """A database's disk counts always; its CPU and memory while it should run."""
    running = database.desired_state == "running" if running is None else running
    disk = int(database.disk_limit_mb or 0)
    if not running:
        return Need(0.0, 0, disk)
    return need_of(database.cpu_limit, database.memory_limit_mb, disk)


# ── What a node gives out, and what is promised on it ────────────────────────

def _meta(node: Node) -> dict:
    try:
        return json.loads(node.metadata_json or "{}")
    except ValueError:
        return {}


def gives_out(node: Node) -> bool:
    return tenant.may_run_on(node)


def allocatable(node: Node, s: Optional[dict] = None) -> Need:
    """What the node may give out: its cores (times cpu_ratio), memory and disk, minus the reserve for the system."""
    s = s or settings()
    if not gives_out(node):
        return Need()
    md = _meta(node)
    cores = float(md.get("cpu_count_logical") or md.get("cpu_count") or 0)
    memory = int(md.get("ram_total_mb") or 0)
    disk = float(md.get("disk_total_gb") or 0)
    return Need(max(0.0, cores - s["reserve_cpu"]) * s["cpu_ratio"], max(0, memory - s["reserve_memory_mb"]),
                max(0, int((disk - s["reserve_disk_gb"]) * 1024)))


async def promised(db: AsyncSession, node_ids: Optional[Iterable[int]] = None, *, exclude_instance: Optional[int] = None,
                   exclude_database: Optional[int] = None) -> dict[int, dict]:
    """Per node: {"need": Need, "biggest": Need of its biggest app instance} of everything promised there."""
    ids = set(node_ids) if node_ids is not None else None
    out: dict[int, dict] = {}

    def add(node_id, need: Need, instance=False):
        if node_id is None or (ids is not None and node_id not in ids):
            return
        entry = out.setdefault(node_id, {"need": Need(), "biggest": Need()})
        entry["need"] = entry["need"] + need
        if instance and need.memory_mb > entry["biggest"].memory_mb:
            entry["biggest"] = need

    rows = (await db.execute(select(Instance, Application).join(Application, Instance.app_id == Application.id)
                             .where(Instance.desired == "running"))).all()
    for instance, app in rows:
        if instance.id != exclude_instance:
            add(instance.node_id, running_need(instance, app), instance=True)
    local_id = (await db.execute(select(Node.id).where(Node.is_local == True))).scalar_one_or_none()   # noqa: E712
    for app in (await db.execute(select(Application).where(Application.cron_schedule.is_not(None)))).scalars():
        if (app.cron_schedule or "").strip():
            add(app.node_id or local_id, app_need(app), instance=True)       # a job may start any time: always promised
    for database in (await db.execute(select(Database))).scalars():
        if database.id != exclude_database:
            add(database.node_id, database_need(database))
    return out


@dataclass
class Room:
    node: Node
    allocatable: Need
    promised: Need
    biggest: Need

    @property
    def free(self) -> Need:
        return self.allocatable - self.promised

    def fits(self, need: Need, *, surge: bool = False, instance: bool = True, s: Optional[dict] = None) -> Optional[str]:
        """None when `need` fits here; otherwise why not. An app instance leaves room for a deploy of the biggest app
        instance here (itself included); one started by a deploy may use that room."""
        s = s or settings()
        if not gives_out(self.node):
            return "nothing runs on the primary"
        keep = Need()
        if s["deploy_room"] and not surge:
            biggest = need if instance and need.memory_mb > self.biggest.memory_mb else self.biggest
            keep = Need(biggest.cpu, biggest.memory_mb, 0)
        left = self.free - keep
        if need.memory_mb > left.memory_mb:
            return f"{need.memory_mb} MB memory asked, {max(0, left.memory_mb)} MB free"
        if need.cpu > left.cpu + 1e-9:
            return f"{need.cpu:g} CPU asked, {max(0.0, left.cpu):.2f} free"
        if need.disk_mb > left.disk_mb:
            return f"{need.disk_mb // 1024} GB disk asked, {max(0, left.disk_mb) // 1024} GB free"
        return None

    def as_dict(self) -> dict:
        return {"allocatable": self.allocatable.as_dict(), "promised": self.promised.as_dict(), "free": self.free.as_dict()}


async def rooms(db: AsyncSession, nodes: Iterable[Node], **exclude) -> list[Room]:
    nodes = list(nodes)
    s = settings()
    taken = await promised(db, [n.id for n in nodes], **exclude)
    return [Room(n, allocatable(n, s), taken.get(n.id, {}).get("need", Need()), taken.get(n.id, {}).get("biggest", Need()))
            for n in nodes]


class NoRoom(HTTPException):
    def __init__(self, detail: str):
        super().__init__(409, detail)


def _no_room(need: Need, reasons: list[str]) -> NoRoom:
    what = f"{need.cpu:g} CPU and {need.memory_mb} MB memory" + (f" and {need.disk_mb // 1024} GB disk" if need.disk_mb else "")
    why = "; ".join(reasons[:4]) or "no node is online"
    return NoRoom(f"No room for {what} on this server's nodes ({why}). Add a node, or make something smaller.")


# ── Choosing and checking ────────────────────────────────────────────────────

async def choose(db: AsyncSession, candidates: Iterable[Node], need: Need, *, spread: Optional[dict] = None,
                 surge: bool = False, instance: bool = True) -> Node:
    """The node to put `need` on: one where it fits, the fewest of `spread` (instances of the same app) first, then the
    most free memory. Without reservations: the fewest of `spread`, then the fewest things, as before."""
    candidates = list(candidates)
    if not candidates:
        raise HTTPException(409, "No node is online to run this")
    spread = spread or {}
    if not enabled():
        return min(candidates, key=lambda n: (spread.get(n.id, 0), 0 if n.is_local else 1, n.id))
    s = settings()
    found, reasons = [], []
    for room in await rooms(db, candidates):
        why = room.fits(need, surge=surge or _surge.get(), instance=instance, s=s)
        (reasons.append(f"{room.node.name}: {why}") if why else found.append(room))
    if not found:
        raise _no_room(need, reasons)
    return min(found, key=lambda r: (spread.get(r.node.id, 0), -r.free.memory_mb, r.node.id)).node


async def choose_many(db: AsyncSession, candidates: Iterable[Node], need: Need, how_many: int, *,
                      spread: Optional[dict] = None) -> list[Node]:
    """Up to `how_many` nodes for instances of `need`, one after the other as choose() would; fewer when the room runs out."""
    s, spread, picked = settings(), dict(spread or {}), []
    found = await rooms(db, candidates)
    for _ in range(how_many):
        fitting = [r for r in found if r.fits(need, s=s) is None]
        if not fitting:
            break
        room = min(fitting, key=lambda r: (spread.get(r.node.id, 0), -r.free.memory_mb, r.node.id))
        picked.append(room.node)
        spread[room.node.id] = spread.get(room.node.id, 0) + 1
        room.promised = room.promised + need
        if need.memory_mb > room.biggest.memory_mb:
            room.biggest = need
    return picked


async def ensure(db: AsyncSession, node: Node, need: Need, *, exclude_instance: Optional[int] = None,
                 exclude_database: Optional[int] = None, surge: bool = False, instance: bool = True) -> None:
    """Raise NoRoom unless `need` fits on this node (what is excluded counts as gone)."""
    if not enabled():
        return
    room = (await rooms(db, [node], exclude_instance=exclude_instance, exclude_database=exclude_database))[0]
    why = room.fits(need, surge=surge or _surge.get(), instance=instance)
    if why:
        raise NoRoom(f"No room on node '{node.name}' for {need.cpu:g} CPU and {need.memory_mb} MB memory ({why}). "
                     "Add a node, or make something smaller.")


async def guard_instance(db: AsyncSession, node_id: int, instance_id: int, payload: dict) -> None:
    """Before an instance gets a new spec (desired.start): it must fit, unless it already ran and asks for no more."""
    if not enabled():
        return
    need = payload_need(payload)
    row = await db.get(Instance, instance_id)
    before = spec_need(row) if row is not None and row.desired == "running" else None
    if before is not None and need.at_most(before):
        return                                           # a restart, or smaller: never refused
    node = await db.get(Node, node_id)
    if node is not None:
        await ensure(db, node, need, exclude_instance=instance_id)


async def check_new_limits(db: AsyncSession, app: Application, cpu, memory_mb) -> None:
    """Before an app's limits change: every instance that should run must fit with them on its node, and a cron job on
    its own. Raises NoRoom (nothing changed yet)."""
    if not enabled():
        return
    instances = (await db.execute(select(Instance).where(Instance.app_id == app.id, Instance.desired == "running"))).scalars().all()
    for instance in instances:
        need = need_of(instance.docker_cpu_limit or cpu, instance.docker_memory_limit_mb or memory_mb)
        before = running_need(instance, app)
        node = await db.get(Node, instance.node_id)
        if node is not None and not need.at_most(before):
            await ensure(db, node, need, exclude_instance=instance.id)      # in its own place; deploys keep their room
    if (app.cron_schedule or "").strip():
        node = await db.get(Node, app.node_id) if app.node_id else (await db.execute(select(Node).where(Node.is_local == True))).scalar_one_or_none()  # noqa: E712
        if node is not None:
            room = (await rooms(db, [node]))[0]
            room.promised = room.promised - app_need(app)
            why = room.fits(need_of(cpu, memory_mb))
            if why:
                raise NoRoom(f"No room on node '{node.name}' for this job's new limits ({why}). Add a node, or keep it smaller.")


async def report(db: AsyncSession) -> dict:
    """The settings, every node's room, and the totals of the nodes that give out (for the panel and for a platform)."""
    nodes = (await db.execute(select(Node).where(Node.enabled == True))).scalars().all()   # noqa: E712
    s = settings()
    per = []
    total_alloc = total_promised = Need()
    for room in await rooms(db, nodes):
        entry = {"node_id": room.node.id, "name": room.node.name, "gives_out": gives_out(room.node),
                 "online": room.node.is_local or room.node.status == "online", **room.as_dict()}
        per.append(entry)
        if entry["gives_out"] and entry["online"]:
            total_alloc, total_promised = total_alloc + room.allocatable, total_promised + room.promised
    return {"settings": s, "enabled": enabled(), "nodes": per,
            "total": {"allocatable": total_alloc.as_dict(), "promised": total_promised.as_dict(),
                      "free": (total_alloc - total_promised).as_dict()}}
