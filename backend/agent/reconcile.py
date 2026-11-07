"""Makes this node run what the primary wants, and tells the primary what it sees.

The primary keeps a list of the instances on this node and how each should run (nodes/desired.py).
This module asks for that list, compares it with the containers Docker really has, and repairs the
difference: start what is missing, recreate what was started with an older spec, remove what is no
longer on the list. It does this again and again, so it does not matter when, or how often, something
goes wrong in between (a restart of the agent in the middle of a build, a lost connection, a crash):
the next pass sees the same difference and continues.

Two rules keep it safe:
* nothing is removed before the list has been received in this run, so an agent that starts while the
  primary is unreachable never mistakes "I know nothing" for "nothing should run";
* a start that fails is retried with a growing delay and given up after a few attempts, until the
  primary changes the spec, so a broken app does not rebuild itself forever.
"""
import asyncio
import time

import httpx

from agent.log import _agent_log
from agent.source import ensure_image
from agent.state import AgentState
from containers import processes as pm, replicas as containers_replicas

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


def runtime_options(spec: dict, bind_host: str) -> dict:
    """The docker options for an instance on this node: its port is published on the node-network address only."""
    options = dict(spec.get("docker_options") or {})
    for source, target in _OPTION_OVERRIDES:
        if spec.get(source) is not None:
            options[target] = spec[source]
    options["bind_host"] = bind_host
    return options


def backoff_seconds(attempts: int) -> int:
    return min(RETRY_BASE_SECONDS * 2 ** max(attempts - 1, 0), RETRY_MAX_SECONDS)


class Reconciler:
    def __init__(self, client: httpx.AsyncClient, state: AgentState):
        self.client, self.state = client, state
        self.list_hash = ""
        self.entries: dict[int, dict] = {}
        self.synced = False
        self.observed: dict[int, dict] = {}
        self.attempts: dict[int, dict] = {}       # replica id -> {hash, count, next, error}
        self.started_at: dict[int, float] = {}
        self.tasks: dict[int, tuple[str, asyncio.Task]] = {}    # replica id -> (spec hash, running start)
        self.pull_now = asyncio.Event()
        self.reconcile_now = asyncio.Event()
        self.report_now = asyncio.Event()

    # ── what the primary wants ───────────────────────────────────────────────
    def nudge(self) -> None:
        """The primary says its list changed."""
        self.pull_now.set()

    async def pull(self) -> None:
        resp = await self.client.get(
            f"{self.state.main_url}/api/nodes/agent/desired", params={"have": self.list_hash},
            headers={"X-Node-Token": self.state.auth_token}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("unchanged"):
            return
        self.entries = {int(e["replica_id"]): e for e in data.get("replicas", [])}
        self.list_hash, self.synced = data["hash"], True
        for replica_id in [r for r in self.observed if r not in self.entries]:
            del self.observed[replica_id]
            self.attempts.pop(replica_id, None)
            self.started_at.pop(replica_id, None)
        _agent_log(f"[reconcile] list received: {sum(e['desired'] == 'running' for e in self.entries.values())} to run, "
                   f"{sum(e['desired'] != 'running' for e in self.entries.values())} stopped")
        self.reconcile_now.set()

    # ── what we see ─────────────────────────────────────────────────────────
    def _set(self, replica_id: int, state: str, **fields) -> None:
        report = {"state": state, "substatus": None, "container_id": None, "spec_hash": None, "error": None, **fields}
        if self.observed.get(replica_id) != report:
            self.observed[replica_id] = report
            self.report_now.set()

    async def report(self) -> None:
        body = {str(rid): obs for rid, obs in self.observed.items()}
        resp = await self.client.post(
            f"{self.state.main_url}/api/nodes/agent/observed", json={"replicas": body},
            headers={"X-Node-Token": self.state.auth_token}, timeout=30)
        resp.raise_for_status()

    # ── making them equal ────────────────────────────────────────────────────
    async def reconcile_once(self) -> None:
        if not self.synced:
            return
        containers = {c["replica_id"]: c for c in await asyncio.to_thread(containers_replicas.list_replica_containers)}

        # 1. whatever should not run, goes
        for replica_id, container in containers.items():
            entry = self.entries.get(replica_id)
            if entry is None or entry["desired"] != "running":
                await self._remove(container)
        containers = {rid: c for rid, c in containers.items()
                      if (self.entries.get(rid) or {}).get("desired") == "running"}

        for replica_id, entry in self.entries.items():
            if entry["desired"] != "running":
                self._set(replica_id, "stopped")
                self._cancel_start(replica_id)
                continue
            self._ensure(entry, containers.get(replica_id))

        # starts that belong to an instance that left the list
        for replica_id in [r for r in self.tasks if r not in self.entries]:
            self._cancel_start(replica_id)

    async def _remove(self, container: dict) -> None:
        replica_id = container["replica_id"]
        self._cancel_start(replica_id)
        _agent_log(f"[reconcile] removing instance {replica_id} ({container['name']}): not wanted anymore")
        try:
            await asyncio.to_thread(pm.stop_docker_replica, container["app_id"], replica_id)
        except Exception as exc:
            _agent_log(f"[reconcile] could not remove {container['name']}: {exc}")

    def _cancel_start(self, replica_id: int) -> None:
        running = self.tasks.pop(replica_id, None)
        if running and not running[1].done():
            running[1].cancel()

    def _ensure(self, entry: dict, container: dict | None) -> None:
        replica_id, wanted = entry["replica_id"], entry["spec_hash"]

        # Already starting this very spec: report what that start is doing
        running = self.tasks.get(replica_id)
        if running and running[0] == wanted and not running[1].done():
            return
        if running:
            self._cancel_start(replica_id)

        up_to_date = container is not None and (container["spec"] in (None, wanted))   # no label: made before desired state
        if container and container["status"] == "running" and up_to_date:
            self.attempts.pop(replica_id, None)
            self._set(replica_id, "running", container_id=container["id"], spec_hash=wanted)
            return
        if container and container["status"] == "restarting" and up_to_date:
            # Docker is restarting a crashing app by itself (its restart policy); not for us to fight
            self._set(replica_id, "error", container_id=container["id"], spec_hash=wanted,
                      error="the app keeps crashing and Docker keeps restarting it; see the logs")
            return

        attempt = self.attempts.get(replica_id)
        if attempt and attempt["hash"] != wanted:
            attempt = self.attempts.pop(replica_id)          # a new spec: a fresh start
            attempt = None
        crashed = (container is not None and up_to_date and container["status"] != "running"
                   and time.monotonic() - self.started_at.get(replica_id, 0) < CRASH_WINDOW_SECONDS)
        if crashed:
            self._failed(replica_id, wanted, f"the container exited right after starting (exit code {container['exit_code']}); see the logs")
            attempt = self.attempts[replica_id]
        if attempt:
            if attempt["count"] >= MAX_ATTEMPTS:
                self._set(replica_id, "error", spec_hash=wanted, error=f"{attempt['error']} (gave up after {MAX_ATTEMPTS} attempts)")
                return
            if time.monotonic() < attempt["next"]:
                self._set(replica_id, "error", spec_hash=wanted, error=attempt["error"])
                return

        self._set(replica_id, "starting", spec_hash=wanted, substatus="waiting")
        task = asyncio.get_running_loop().create_task(self._start(entry, container))
        self.tasks[replica_id] = (wanted, task)

    def _failed(self, replica_id: int, spec_hash: str, message: str) -> None:
        previous = self.attempts.get(replica_id)
        count = (previous["count"] if previous and previous["hash"] == spec_hash else 0) + 1
        self.attempts[replica_id] = {"hash": spec_hash, "count": count, "error": message,
                                     "next": time.monotonic() + backoff_seconds(count)}
        self._set(replica_id, "error", spec_hash=spec_hash, error=message)
        _agent_log(f"[reconcile] instance {replica_id}: {message} (attempt {count}/{MAX_ATTEMPTS})")

    async def _start(self, entry: dict, container: dict | None) -> None:
        replica_id, wanted, spec = entry["replica_id"], entry["spec_hash"], entry["spec"]
        try:
            if not self.state.wg_ip:
                # Not a failure of the app: the node network is still being set up
                self._set(replica_id, "starting", spec_hash=wanted, substatus="waiting_for_network")
                _agent_log(f"[reconcile] instance {replica_id}: waiting for the node network")
                await asyncio.sleep(RECONCILE_SECONDS)
                return
            app_id = int(spec["app_id"])

            def progress(substatus: str) -> None:
                self._set(replica_id, "building", spec_hash=wanted, substatus=substatus)

            await ensure_image(self.client, self.state, spec, progress)
            self._set(replica_id, "starting", spec_hash=wanted, substatus="creating_container")
            if container is not None:
                await asyncio.to_thread(pm.stop_docker_replica, app_id, replica_id)      # another spec, or exited
            container_id = await asyncio.to_thread(
                pm.start_docker_replica, app_id, replica_id, spec.get("app_name") or "",
                spec.get("internal_port", 8000), spec["external_port"], spec.get("env_vars") or {},
                {**runtime_options(spec, self.state.wg_ip), "spec_hash": wanted}, app_id)
            self.started_at[replica_id] = time.monotonic()
            self.attempts.pop(replica_id, None)
            self._set(replica_id, "running", container_id=container_id, spec_hash=wanted)
            _agent_log(f"[reconcile] instance {replica_id} started ({container_id[:12]})")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failed(replica_id, wanted, str(exc) or type(exc).__name__)
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
                _agent_log(f"[reconcile] {what} failed ({type(exc).__name__}: {exc})")
            await self._wait(event, seconds)

    async def run(self) -> None:
        """Runs for as long as the agent does."""
        await asyncio.gather(
            self._every(self.pull_now, PULL_SECONDS, self.pull, "asking for the list"),
            self._every(self.reconcile_now, RECONCILE_SECONDS, self.reconcile_once, "reconciling"),
            self._every(self.report_now, REPORT_SECONDS, self.report, "reporting"),
        )
