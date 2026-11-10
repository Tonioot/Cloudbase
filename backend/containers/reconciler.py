"""Makes a machine run what the primary wants, and tells the primary what it sees.

The primary keeps a list of the instances on a node and how each should run (nodes/desired.py). This module
compares that list with the containers Docker really has and repairs the difference: start what is missing,
recreate what was started with an older spec, remove what is no longer on the list. It does this again and
again, so it does not matter when, or how often, something goes wrong in between (a restart in the middle of
a build, a lost connection, a crash): the next pass sees the same difference and continues.

Two rules keep it safe:
* nothing is removed before the list has been received in this run, so a machine that starts while the
  primary is unreachable never mistakes "I know nothing" for "nothing should run";
* a start that fails is retried with a growing delay and given up after a few attempts, until the
  primary changes the spec, so a broken app does not rebuild itself forever.

How the list arrives and where the report goes differs per machine, and is left to the subclass: the agent on a
node talks HTTP (agent/reconcile.py), the primary reads and writes its own database (nodes/local.py).
"""
import asyncio
import logging
import time

from containers import processes as pm, instances as containers_instances

_logger = logging.getLogger("cloudbase.agent")      # the node-agent log, also where the primary's reconciler writes


def _log(message: str) -> None:
    _logger.info(message)

PULL_SECONDS = 10          # how often to ask for the list (the primary also nudges when it changes)
RECONCILE_SECONDS = 10     # how often to compare with Docker when nothing was said
REPORT_SECONDS = 15        # how often to tell the primary what we see, also when nothing changed
MAX_ATTEMPTS = 5
RETRY_BASE_SECONDS = 30
RETRY_MAX_SECONDS = 600
CRASH_WINDOW_SECONDS = 120  # a container that exits this soon after being started has crashed

# instance option names in the spec (set per instance) -> name in the docker options
_OPTION_OVERRIDES = (
    ("docker_cpu_limit", "cpu_limit"), ("docker_memory_limit_mb", "memory_limit_mb"),
    ("docker_read_only_root", "read_only_root"), ("docker_tmpfs_enabled", "tmpfs_enabled"),
    ("docker_tmpfs_size_mb", "tmpfs_size_mb"),
)


def runtime_options(spec: dict, bind_host: str | None) -> dict:
    """The docker options for an instance: a node publishes its port on the node-network address only."""
    options = dict(spec.get("docker_options") or {})
    for source, target in _OPTION_OVERRIDES:
        if spec.get(source) is not None:
            options[target] = spec[source]
    if bind_host:
        options["bind_host"] = bind_host      # without one the spec decides (the primary: localhost when nginx is in front)
    return options


def backoff_seconds(attempts: int) -> int:
    return min(RETRY_BASE_SECONDS * 2 ** max(attempts - 1, 0), RETRY_MAX_SECONDS)


class Reconciler:
    def __init__(self):
        self.list_hash = ""
        self.entries: dict[int, dict] = {}
        self.synced = False
        self.observed: dict[int, dict] = {}
        self.attempts: dict[int, dict] = {}       # instance id -> {hash, count, next, error}
        self.started_at: dict[int, float] = {}
        self.tasks: dict[int, tuple[str, asyncio.Task]] = {}    # instance id -> (spec hash, running start)
        self.pull_now = asyncio.Event()
        self.reconcile_now = asyncio.Event()
        self.report_now = asyncio.Event()

    # ── how this machine talks to the primary: the subclass says ──────────────
    async def _fetch_list(self) -> dict:
        """The list of instances for this node, or {"hash": ..., "unchanged": True} when it is the one we have."""
        raise NotImplementedError

    async def _send_report(self, body: dict) -> None:
        """Tell the primary what we see: {instance id: observation}."""
        raise NotImplementedError

    def _network_ready(self) -> bool:
        """False while the machine cannot take instances yet (a node still joining the node network)."""
        raise NotImplementedError

    def _bind_host(self) -> str | None:
        """The address instances publish their port on; None leaves it to the spec."""
        raise NotImplementedError

    async def _prepare(self, spec: dict, progress) -> None:
        """Get the image for the spec onto this machine."""
        raise NotImplementedError

    # ── what the primary wants ───────────────────────────────────────────────
    def nudge(self) -> None:
        """The primary says its list changed."""
        self.pull_now.set()

    async def pull(self) -> None:
        data = await self._fetch_list()
        if data.get("unchanged"):
            return
        # Strict on purpose: an answer without "instances" (another version, an error page) is not an empty list,
        # and taking it for one would remove every container here
        self.entries = {int(e["instance_id"]): e for e in data["instances"]}
        self.list_hash, self.synced = data["hash"], True
        for instance_id in [r for r in self.observed if r not in self.entries]:
            del self.observed[instance_id]
            self.attempts.pop(instance_id, None)
            self.started_at.pop(instance_id, None)
        _log(f"[reconcile] list received: {sum(e['desired'] == 'running' for e in self.entries.values())} to run, "
                   f"{sum(e['desired'] != 'running' for e in self.entries.values())} stopped")
        self.reconcile_now.set()

    # ── what we see ─────────────────────────────────────────────────────────
    def _set(self, instance_id: int, state: str, **fields) -> None:
        report = {"state": state, "substatus": None, "container_id": None, "spec_hash": None, "error": None, **fields}
        if self.observed.get(instance_id) != report:
            self.observed[instance_id] = report
            self.report_now.set()

    async def report(self) -> None:
        await self._send_report({str(rid): obs for rid, obs in self.observed.items()})

    # ── making them equal ────────────────────────────────────────────────────
    async def reconcile_once(self) -> None:
        if not self.synced:
            return
        containers = {c["instance_id"]: c for c in await asyncio.to_thread(containers_instances.list_instance_containers)}

        # 1. whatever should not run, goes. All at once: stopping a container takes seconds, and one that is slow
        # must not hold up the others (nor the report that the others are gone)
        doomed = [c for rid, c in containers.items()
                  if (self.entries.get(rid) or {}).get("desired") != "running"]
        removed = await asyncio.gather(*(self._remove(c) for c in doomed))
        stuck = {c["instance_id"] for c, gone in zip(doomed, removed) if not gone}
        containers = {rid: c for rid, c in containers.items()
                      if (self.entries.get(rid) or {}).get("desired") == "running"}

        for instance_id, entry in self.entries.items():
            if entry["desired"] != "running":
                if instance_id not in stuck:                 # never say "stopped" about a container that is still there
                    self._set(instance_id, "stopped")
                self._cancel_start(instance_id)
                continue
            self._ensure(entry, containers.get(instance_id))

        # starts that belong to an instance that left the list
        for instance_id in [r for r in self.tasks if r not in self.entries]:
            self._cancel_start(instance_id)

    async def _remove(self, container: dict) -> bool:
        """Remove the container of an instance that should not run. Returns whether it is gone."""
        instance_id = container["instance_id"]
        self._cancel_start(instance_id)
        _log(f"[reconcile] removing instance {instance_id} ({container['name']}): not wanted anymore")
        began = time.monotonic()
        try:
            gone = await asyncio.to_thread(pm.stop_docker_instance, container["app_id"], instance_id)
        except Exception as exc:
            _log(f"[reconcile] could not remove {container['name']}: {exc}")
            return False
        took = time.monotonic() - began
        if gone is False:
            _log(f"[reconcile] {container['name']} is still there after {took:.0f}s; trying again next time")
        elif took > 15:
            _log(f"[reconcile] removing {container['name']} took {took:.0f}s")
        return gone is not False

    def _cancel_start(self, instance_id: int) -> None:
        running = self.tasks.pop(instance_id, None)
        if running and not running[1].done():
            running[1].cancel()

    def _ensure(self, entry: dict, container: dict | None) -> None:
        instance_id, wanted = entry["instance_id"], entry["spec_hash"]

        # Already starting this very spec: report what that start is doing
        running = self.tasks.get(instance_id)
        if running and running[0] == wanted and not running[1].done():
            return
        if running:
            self._cancel_start(instance_id)

        up_to_date = container is not None and container["spec"] == wanted
        if container and container["status"] == "running" and up_to_date:
            self.attempts.pop(instance_id, None)
            self._set(instance_id, "running", container_id=container["id"], spec_hash=wanted)
            return
        if container and container["status"] == "restarting" and up_to_date:
            # Docker is restarting a crashing app by itself (its restart policy); not for us to fight
            self._set(instance_id, "error", container_id=container["id"], spec_hash=wanted,
                      error="the app keeps crashing and Docker keeps restarting it; see the logs")
            return

        attempt = self.attempts.get(instance_id)
        if attempt and attempt["hash"] != wanted:
            attempt = self.attempts.pop(instance_id)          # a new spec: a fresh start
            attempt = None
        # Only a start made by this agent can have "crashed right away": without one (after a restart of the agent or of
        # the whole server) an exited container is simply started again
        started = self.started_at.get(instance_id)
        crashed = (container is not None and up_to_date and container["status"] != "running"
                   and started is not None and time.monotonic() - started < CRASH_WINDOW_SECONDS)
        if crashed:
            self._failed(instance_id, wanted, f"the container exited right after starting (exit code {container['exit_code']}); see the logs")
            attempt = self.attempts[instance_id]
        if attempt:
            if attempt["count"] >= MAX_ATTEMPTS:
                self._set(instance_id, "error", spec_hash=wanted, error=f"{attempt['error']} (gave up after {MAX_ATTEMPTS} attempts)")
                return
            if time.monotonic() < attempt["next"]:
                self._set(instance_id, "error", spec_hash=wanted, error=attempt["error"])
                return

        self._set(instance_id, "starting", spec_hash=wanted, substatus="waiting")
        task = asyncio.get_running_loop().create_task(self._start(entry, container))
        self.tasks[instance_id] = (wanted, task)

    def _failed(self, instance_id: int, spec_hash: str, message: str) -> None:
        previous = self.attempts.get(instance_id)
        count = (previous["count"] if previous and previous["hash"] == spec_hash else 0) + 1
        self.attempts[instance_id] = {"hash": spec_hash, "count": count, "error": message,
                                     "next": time.monotonic() + backoff_seconds(count)}
        self._set(instance_id, "error", spec_hash=spec_hash, error=message)
        _log(f"[reconcile] instance {instance_id}: {message} (attempt {count}/{MAX_ATTEMPTS})")

    async def _start(self, entry: dict, container: dict | None) -> None:
        instance_id, wanted, spec = entry["instance_id"], entry["spec_hash"], entry["spec"]
        try:
            if not self._network_ready():
                # Not a failure of the app: the node network is still being set up
                self._set(instance_id, "starting", spec_hash=wanted, substatus="waiting_for_network")
                _log(f"[reconcile] instance {instance_id}: waiting for the node network")
                await asyncio.sleep(RECONCILE_SECONDS)
                return
            app_id = int(spec["app_id"])

            def progress(substatus: str) -> None:
                self._set(instance_id, "building", spec_hash=wanted, substatus=substatus)

            await self._prepare(spec, progress)
            self._set(instance_id, "starting", spec_hash=wanted, substatus="creating_container")
            if container is not None:
                await asyncio.to_thread(pm.stop_docker_instance, app_id, instance_id)      # another spec, or exited
            container_id = await asyncio.to_thread(
                pm.start_docker_instance, app_id, instance_id,
                spec.get("internal_port", 8000), spec["external_port"], spec.get("env_vars") or {},
                {**runtime_options(spec, self._bind_host()), "spec_hash": wanted})
            self.started_at[instance_id] = time.monotonic()
            self.attempts.pop(instance_id, None)
            self._set(instance_id, "running", container_id=container_id, spec_hash=wanted)
            _log(f"[reconcile] instance {instance_id} started ({container_id[:12]})")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failed(instance_id, wanted, str(exc) or type(exc).__name__)
        finally:
            self.reconcile_now.set()

    # ── the loops ────────────────────────────────────────────────────────────
    @staticmethod
    async def _wait(event: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        event.clear()

    async def _every(self, event: asyncio.Event, seconds: float, step, what: str) -> None:
        while True:
            try:
                await step()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log(f"[reconcile] {what} failed ({type(exc).__name__}: {exc})")
            await self._wait(event, seconds)

    async def run(self) -> None:
        """Runs for as long as the agent does."""
        await asyncio.gather(
            self._every(self.pull_now, PULL_SECONDS, self.pull, "asking for the list"),
            self._every(self.reconcile_now, RECONCILE_SECONDS, self.reconcile_once, "reconciling"),
            self._every(self.report_now, REPORT_SECONDS, self.report, "reporting"),
        )
