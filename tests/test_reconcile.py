"""The node's side of desired state: make Docker equal to the primary's list (containers/reconciler.py; agent/reconcile.py is its HTTP side)."""
import asyncio

import pytest

from agent import reconcile
from agent.reconcile import Reconciler
from agent.state import AgentState
from containers import processes as pm, reconciler as engine, replicas as containers_replicas


def _run(coro):
    return asyncio.run(coro)


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class FakeClient:
    """The primary, as far as the agent can see: answers the list, remembers the reports."""

    def __init__(self, replicas=None, hash_="h1"):
        self.replicas, self.hash = replicas or [], hash_
        self.reports, self.asked = [], []

    async def get(self, url, params=None, headers=None, timeout=None):
        assert url.endswith("/api/nodes/agent/desired") and headers == {"X-Node-Token": "t"}
        self.asked.append(params)
        if params and params.get("have") == self.hash:
            return FakeResponse({"hash": self.hash, "unchanged": True})
        return FakeResponse({"hash": self.hash, "replicas": self.replicas})

    async def post(self, url, json=None, headers=None, timeout=None):
        assert url.endswith("/api/nodes/agent/observed") and headers == {"X-Node-Token": "t"}
        self.reports.append(json["replicas"])
        return FakeResponse({"ok": True})


def entry(replica_id, desired="running", spec_hash="aaaa", **spec):
    base = {"app_id": 1, "app_name": "shop", "internal_port": 3000, "external_port": 9100 + replica_id,
            "env_vars": {"K": "v"}, "source_revision": "rev1", "docker_options": {"restart_policy": "no", "publish_port": True}}
    return {"replica_id": replica_id, "app_id": 1, "desired": desired,
            "spec_hash": spec_hash if desired == "running" else None, "spec": {**base, **spec} if desired == "running" else None}


def container(replica_id, status="running", spec="aaaa", exit_code=0):
    return {"name": f"cloudbase-app-1-replica-{replica_id}", "id": f"cid{replica_id}" + "0" * 20, "app_id": 1,
            "replica_id": replica_id, "status": status, "spec": spec, "exit_code": exit_code}


class Docker:
    """A stand-in for Docker on this node."""

    def __init__(self, monkeypatch, containers=()):
        self.containers = {c["replica_id"]: dict(c) for c in containers}
        self.removed, self.started, self.built = [], [], []
        monkeypatch.setattr(containers_replicas, "list_replica_containers", lambda: [dict(c) for c in self.containers.values()])
        monkeypatch.setattr(pm, "stop_docker_replica", self._stop)
        monkeypatch.setattr(pm, "start_docker_replica", self._start)
        monkeypatch.setattr(reconcile, "ensure_image", self._ensure_image)
        self.fail_start = None

    def _stop(self, app_id, replica_id):
        self.removed.append(replica_id)
        self.containers.pop(replica_id, None)
        return True

    def _start(self, app_id, replica_id, internal_port, external_port, env, options):
        if self.fail_start:
            raise RuntimeError(self.fail_start)
        self.started.append({"replica_id": replica_id, "port": external_port, "env": env, "options": options, "internal": internal_port})
        self.containers[replica_id] = container(replica_id, spec=options["spec_hash"])
        return self.containers[replica_id]["id"]

    async def _ensure_image(self, client, state, spec, progress):
        progress("downloading")
        progress("building_image")
        self.built.append(spec["app_id"])


def make(monkeypatch, entries, containers=(), wg_ip="10.77.0.2"):
    docker = Docker(monkeypatch, containers)
    client = FakeClient(entries)
    state = AgentState(main_url="https://p", auth_token="t", node_id=1, node_name="n", wg_ip=wg_ip)
    return Reconciler(client, state), client, docker


async def settle(rec):
    """Let the starts that a pass began finish."""
    for _, task in list(rec.tasks.values()):
        await task


async def converge(rec, passes=1):
    for _ in range(passes):
        await rec.reconcile_once()
        await settle(rec)


def state_of(rec, replica_id):
    return rec.observed[replica_id]["state"]


# ── getting the list ──────────────────────────────────────────────────────────
def test_the_list_is_asked_for_with_the_hash_of_the_one_we_have(monkeypatch):
    rec, client, _ = make(monkeypatch, [entry(1)])

    async def go():
        await rec.pull()
        await rec.pull()
    _run(go())
    assert client.asked == [{"have": ""}, {"have": "h1"}] and rec.synced and set(rec.entries) == {1}


def test_an_instance_that_leaves_the_list_is_forgotten(monkeypatch):
    rec, client, _ = make(monkeypatch, [entry(1), entry(2)])

    async def go():
        await rec.pull()
        rec.observed[2] = {"state": "running"}
        client.replicas, client.hash = [entry(1)], "h2"
        await rec.pull()
    _run(go())
    assert set(rec.entries) == {1} and 2 not in rec.observed


# ── safety ─────────────────────────────────────────────────────────────────────
def test_nothing_is_touched_before_the_list_has_been_received(monkeypatch):
    rec, _, docker = make(monkeypatch, [], containers=[container(7)])
    _run(rec.reconcile_once())
    assert docker.removed == [] and docker.started == []              # "I know nothing" is not "nothing should run"


def test_a_container_the_primary_does_not_know_is_removed(monkeypatch):
    rec, _, docker = make(monkeypatch, [entry(1)], containers=[container(1), container(7)])

    async def go():
        await rec.pull()
        await converge(rec)
    _run(go())
    assert docker.removed == [7] and state_of(rec, 1) == "running"


def test_an_instance_that_should_stop_is_removed_and_reported_stopped(monkeypatch):
    rec, _, docker = make(monkeypatch, [entry(1, desired="stopped")], containers=[container(1)])

    async def go():
        await rec.pull()
        await converge(rec)
    _run(go())
    assert docker.removed == [1] and state_of(rec, 1) == "stopped" and docker.started == []


# ── starting ───────────────────────────────────────────────────────────────────
def test_a_missing_instance_is_built_and_started_on_the_node_network_address(monkeypatch):
    rec, _, docker = make(monkeypatch, [entry(1, docker_cpu_limit=0.5, docker_memory_limit_mb=256)])

    async def go():
        await rec.pull()
        await converge(rec)
    _run(go())
    started = docker.started[0]
    assert docker.built == [1] and started["port"] == 9101 and started["env"] == {"K": "v"} and started["internal"] == 3000
    assert started["options"]["bind_host"] == "10.77.0.2"                     # never on the public address
    assert started["options"]["spec_hash"] == "aaaa"                          # so the next pass knows which start this is
    assert started["options"]["cpu_limit"] == 0.5 and started["options"]["memory_limit_mb"] == 256
    assert started["options"]["restart_policy"] == "no"
    obs = rec.observed[1]
    assert obs["state"] == "running" and obs["spec_hash"] == "aaaa" and obs["container_id"].startswith("cid1")


def test_progress_is_reported_while_building(monkeypatch):
    rec, _, docker = make(monkeypatch, [entry(1)])
    seen = []
    original = docker._ensure_image

    async def watching(client, state, spec, progress):
        await original(client, state, spec, lambda step: (progress(step), seen.append(dict(rec.observed[1]))))
    monkeypatch.setattr(reconcile, "ensure_image", watching)

    async def go():
        await rec.pull()
        await converge(rec)
    _run(go())
    assert [(o["state"], o["substatus"]) for o in seen] == [("building", "downloading"), ("building", "building_image")]


def test_a_container_that_is_up_to_date_is_left_alone(monkeypatch):
    rec, _, docker = make(monkeypatch, [entry(1)], containers=[container(1, spec="aaaa")])

    async def go():
        await rec.pull()
        await converge(rec, passes=3)
    _run(go())
    assert docker.removed == [] and docker.started == [] and docker.built == [] and state_of(rec, 1) == "running"


def test_a_container_from_before_desired_state_is_adopted_not_recreated(monkeypatch):
    rec, _, docker = make(monkeypatch, [entry(1)], containers=[container(1, spec=None)])

    async def go():
        await rec.pull()
        await converge(rec)
    _run(go())
    assert docker.removed == [] and docker.started == [] and state_of(rec, 1) == "running"


def test_a_new_spec_recreates_the_container(monkeypatch):
    rec, client, docker = make(monkeypatch, [entry(1, spec_hash="aaaa")], containers=[container(1, spec="aaaa")])

    async def go():
        await rec.pull()
        await converge(rec)
        client.replicas, client.hash = [entry(1, spec_hash="bbbb")], "h2"          # the primary restarted it
        await rec.pull()
        await converge(rec)
    _run(go())
    assert docker.removed == [1] and [s["options"]["spec_hash"] for s in docker.started] == ["bbbb"]


def test_a_container_that_stopped_is_started_again(monkeypatch):
    rec, _, docker = make(monkeypatch, [entry(1)], containers=[container(1, status="exited", exit_code=137)])

    async def go():
        await rec.pull()
        await converge(rec)
    _run(go())
    assert docker.removed == [1] and len(docker.started) == 1 and state_of(rec, 1) == "running"


def test_a_container_that_dockers_own_restart_policy_is_restarting_is_not_fought(monkeypatch):
    rec, _, docker = make(monkeypatch, [entry(1)], containers=[container(1, status="restarting")])

    async def go():
        await rec.pull()
        await converge(rec)
    _run(go())
    assert docker.removed == [] and docker.started == []
    assert state_of(rec, 1) == "error" and "keeps crashing" in rec.observed[1]["error"]


def test_a_container_that_dies_right_after_starting_is_a_crash_with_a_delay(monkeypatch):
    rec, _, docker = make(monkeypatch, [entry(1)])

    async def go():
        await rec.pull()
        await converge(rec)                                   # started
        docker.containers[1]["status"] = "exited"
        docker.containers[1]["exit_code"] = 1
        await converge(rec)                                   # exited within the window: crash, not restarted at once
        return list(docker.started)
    started = _run(go())
    assert len(started) == 1 and state_of(rec, 1) == "error" and "exit code 1" in rec.observed[1]["error"]
    assert rec.attempts[1]["count"] == 1


# ── failing ───────────────────────────────────────────────────────────────────
def test_a_failed_start_is_reported_and_retried_later_not_at_once(monkeypatch):
    rec, _, docker = make(monkeypatch, [entry(1)])
    docker.fail_start = "no space left on device"

    async def go():
        await rec.pull()
        await converge(rec, passes=3)
    _run(go())
    assert len(docker.built) == 1                                        # tried once, then waits
    assert state_of(rec, 1) == "error" and "no space left" in rec.observed[1]["error"]


def test_the_retry_comes_when_the_delay_is_over_and_stops_after_a_few_attempts(monkeypatch):
    rec, _, docker = make(monkeypatch, [entry(1)])
    docker.fail_start = "boom"

    async def go():
        await rec.pull()
        for _ in range(engine.MAX_ATTEMPTS + 3):
            await converge(rec)
            if 1 in rec.attempts:
                rec.attempts[1]["next"] = 0                                 # the delay has passed
    _run(go())
    assert rec.attempts[1]["count"] == engine.MAX_ATTEMPTS and len(docker.built) == engine.MAX_ATTEMPTS
    assert "gave up" in rec.observed[1]["error"]


def test_a_new_spec_gets_a_fresh_start_after_giving_up(monkeypatch):
    rec, client, docker = make(monkeypatch, [entry(1, spec_hash="aaaa")])
    docker.fail_start = "boom"

    async def go():
        await rec.pull()
        await converge(rec)
        rec.attempts[1]["count"] = engine.MAX_ATTEMPTS
        docker.fail_start = None
        client.replicas, client.hash = [entry(1, spec_hash="bbbb")], "h2"
        await rec.pull()
        await converge(rec)
    _run(go())
    assert state_of(rec, 1) == "running" and 1 not in rec.attempts


def test_backoff_grows_and_has_a_ceiling():
    delays = [engine.backoff_seconds(n) for n in range(1, 10)]
    assert delays[0] == engine.RETRY_BASE_SECONDS and delays == sorted(delays) and delays[-1] == engine.RETRY_MAX_SECONDS


def test_without_a_node_network_address_nothing_is_started_and_nothing_counts_as_failed(monkeypatch):
    rec, _, docker = make(monkeypatch, [entry(1)], wg_ip="")
    monkeypatch.setattr(engine, "RECONCILE_SECONDS", 0)

    async def go():
        await rec.pull()
        await converge(rec)
    _run(go())
    assert docker.started == [] and docker.built == [] and 1 not in rec.attempts
    assert rec.observed[1]["substatus"] == "waiting_for_network"


# ── the report ──────────────────────────────────────────────────────────────────
def test_what_we_see_is_sent_to_the_primary(monkeypatch):
    rec, client, _ = make(monkeypatch, [entry(1), entry(2, desired="stopped")], containers=[container(1)])

    async def go():
        await rec.pull()
        await converge(rec)
        await rec.report()
    _run(go())
    assert client.reports[-1]["1"]["state"] == "running" and client.reports[-1]["2"]["state"] == "stopped"


def test_a_change_makes_the_report_go_out_at_once(monkeypatch):
    rec, _, _ = make(monkeypatch, [entry(1)])
    rec.report_now.clear()
    rec._set(1, "starting", spec_hash="aaaa")
    assert rec.report_now.is_set()
    rec.report_now.clear()
    rec._set(1, "starting", spec_hash="aaaa")                            # nothing new: no extra report
    assert not rec.report_now.is_set()


def test_the_primary_can_make_the_agent_look_at_its_list_at_once(monkeypatch):
    rec, _, _ = make(monkeypatch, [])
    rec.nudge()
    assert rec.pull_now.is_set()


# ── the options ───────────────────────────────────────────────────────────────────
def test_runtime_options_publish_on_the_node_network_address_and_honour_instance_limits():
    options = engine.runtime_options({"docker_options": {"bind_host": "127.0.0.1", "cpu_limit": 2}, "docker_cpu_limit": 0.5,
                                         "docker_read_only_root": True, "docker_memory_limit_mb": None}, "10.77.0.9")
    assert options["bind_host"] == "10.77.0.9" and options["cpu_limit"] == 0.5 and options["read_only_root"] is True
    assert "memory_limit_mb" not in options


# ── the whole agent no longer talks to its own server ─────────────────────────────────────────
def test_the_agent_does_not_call_the_local_api():
    from pathlib import Path
    agent = Path(__file__).resolve().parent.parent / "backend" / "agent"
    for path in agent.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "_LOCAL_API_BASE" not in text and "127.0.0.1:{" not in text, path.name
        assert "run-remote" not in text and "stop-remote" not in text, path.name


@pytest.mark.parametrize("command", ["start_replica", "stop_replica", "refresh_source", "delete_app"])
def test_the_commands_that_desired_state_replaced_are_gone_from_the_agent(command):
    from agent.dispatch import COMMAND_HANDLERS
    assert command not in COMMAND_HANDLERS


@pytest.mark.parametrize("uptime", [5.0, 60.0, 119.0, 100000.0])
def test_an_exited_container_is_started_again_whatever_the_uptime_of_the_machine(monkeypatch, uptime):
    """Right after a boot (a fresh CI runner, a node that just restarted) the clock is small: that must not make an
    exited container look like a crash of a start this agent made, which it did not."""
    import time
    monkeypatch.setattr(time, "monotonic", lambda: uptime)
    rec, _, docker = make(monkeypatch, [entry(1)], containers=[container(1, status="exited", exit_code=137)])

    async def go():
        await rec.pull()
        await converge(rec)
    _run(go())
    assert docker.removed == [1] and len(docker.started) == 1 and state_of(rec, 1) == "running"


# ── removing what should not run ──────────────────────────────────────────────
def test_containers_are_removed_at_the_same_time(monkeypatch):
    """One slow stop must not make the other instances (and their "stopped" report) wait for it."""
    import time
    rec, _, docker = make(monkeypatch, [entry(1, desired="stopped"), entry(2, desired="stopped")], [container(1), container(2)])
    stop = docker._stop

    def slow(app_id, replica_id):
        time.sleep(0.4)
        return stop(app_id, replica_id)
    monkeypatch.setattr(pm, "stop_docker_replica", slow)

    async def go():
        await rec.pull()
        began = time.monotonic()
        await rec.reconcile_once()
        return time.monotonic() - began
    took = _run(go())
    assert took < 0.7 and sorted(docker.removed) == [1, 2]
    assert state_of(rec, 1) == "stopped" and state_of(rec, 2) == "stopped"


def test_a_container_that_could_not_be_stopped_is_not_reported_as_stopped(monkeypatch):
    rec, _, docker = make(monkeypatch, [entry(1, desired="stopped")], [container(1)])
    monkeypatch.setattr(pm, "stop_docker_replica", lambda app_id, replica_id: False)

    async def go():
        await rec.pull()
        await rec.reconcile_once()
    _run(go())
    assert 1 not in rec.observed          # the next pass tries again; the primary is not told something untrue
