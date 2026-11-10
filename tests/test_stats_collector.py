"""The stats collector asks Docker for each instance once per round, and only as often as somebody needs the numbers.

A Docker stats call takes about a second and runs in a thread. Running one per instance every 2 s (twice, in fact),
whether or not anyone looked, saturated the thread pool at a few dozen instances on the primary.

The rounds are tested on plain objects, not on database rows: the app these tests run in has its own collector
running, which looks at the database and must not see (or be seen by) the tests.
"""
import asyncio
import contextvars
import threading
import time
from types import SimpleNamespace as N

import pytest

_MINE = contextvars.ContextVar("test-round", default=False)     # copied into the threads of asyncio.to_thread


def _run(coro):
    return asyncio.run(coro)


def _app(app_id, node_id=None):
    return N(id=app_id, node_id=node_id)


def _instances(app_id, count):
    return [N(id=app_id * 1000 + i, app_id=app_id, node_id=None) for i in range(count)]


@pytest.fixture
def docker(monkeypatch):
    """Docker stats, faked: remembers which containers the test's own rounds asked for, and how many at once."""
    from containers import processes as pm
    from workers import stats

    class Fake:
        def __init__(self):
            self.asked, self.busy, self.busiest, self.lock = [], 0, 0, threading.Lock()

        def __call__(self, name):
            if not _MINE.get():                                   # the app's own collector, not the test
                return {}
            with self.lock:
                self.asked.append(name)
                self.busy += 1
                self.busiest = max(self.busiest, self.busy)
            time.sleep(0.02)
            with self.lock:
                self.busy -= 1
            return {"cpu_percent": 10.0, "memory_mb": 50.0, "memory_vms_mb": 60.0, "net_rx_mb": 1.0, "net_tx_mb": 1.0,
                    "disk_read_mb": 0.0, "disk_write_mb": 0.0, "uptime_seconds": 5}

        def asked_for(self, app_id):
            return [n for n in self.asked if n.startswith(f"cloudbase-app-{app_id}-")]

    fake = Fake()
    monkeypatch.setattr(stats.containers_stats_logs, "get_container_stats_by_name", fake)
    monkeypatch.setattr(pm, "_stats_queues", {})
    return fake


class Round:
    """One round of the collector over these apps."""

    def __init__(self, apps_and_instances):
        self.apps = [a for a, _ in apps_and_instances]
        self.instances = {a.id: r for a, r in apps_and_instances}

    def run(self, local_node_id=None):
        from workers import stats

        async def go():
            _MINE.set(True)
            return await stats.sample_apps(self.apps, self.instances, local_node_id)
        return _run(go())


def test_every_instance_is_asked_once_per_round(docker):
    from containers import processes as pm
    app_id = 900001
    instances = _instances(app_id, 3)
    assert Round([(_app(app_id), instances)]).run() == 3
    assert len(docker.asked_for(app_id)) == 3                             # not twice each, as it used to be
    assert all(pm.get_all_instance_stats(app_id, [r.id])[r.id]["cpu_percent"] == 10.0 for r in instances)
    frames = pm.get_recent_stats(app_id)
    assert len(frames) == 1 and frames[0]["memory_mb"] == 150.0 and frames[0]["cpu_percent"] == 10.0


def test_every_app_is_sampled_every_round_whether_anybody_looks_or_not(docker):
    """One rhythm: nothing has to know who is looking."""
    app_id = 900002
    rounds = Round([(_app(app_id), _instances(app_id, 2))])
    rounds.run()
    rounds.run()
    assert len(docker.asked_for(app_id)) == 4


def test_what_runs_here_is_sampled_wherever_else_the_app_runs(docker):
    """Only this server's instances are handed in; an app with instances elsewhere too is no reason to skip them."""
    app_id = 900005
    assert Round([(_app(app_id, node_id=77), _instances(app_id, 2))]).run() == 2
    assert len(docker.asked_for(app_id)) == 2


def test_an_app_without_running_instances_here_is_not_asked_about(docker):
    app_id = 900006
    assert Round([(_app(app_id), [])]).run() == 0
    assert docker.asked_for(app_id) == []


def test_many_instances_never_run_more_stats_calls_at_once_than_the_limit(docker):
    from workers import stats
    app_id = 900007
    assert Round([(_app(app_id), _instances(app_id, 40))]).run() == 40
    assert len(docker.asked_for(app_id)) == 40
    assert docker.busiest <= stats.STATS_CONCURRENCY


def test_there_is_one_collector_for_every_instance():
    """No separate poller for other nodes, no stream per open stats tab, no 'who is watching': one round, every 5 s."""
    from containers import processes as pm
    from workers import stats
    import lifespan
    assert not hasattr(stats, "_remote_instance_stats_poller") and not hasattr(pm, "is_watched") and stats.SAMPLE_SECONDS == 5
    assert "_remote_instance_stats_poller" not in open(lifespan.__file__, encoding="utf-8").read()


def test_instances_on_other_nodes_use_what_their_node_sent_and_make_one_frame_with_the_rest(docker):
    """One frame per app over all its instances. Nothing is asked of the other node: it sends its numbers itself."""
    from containers import processes as pm
    from workers import stats
    app_id = 900020
    here = N(id=9000201, app_id=app_id, node_id=1)
    there = N(id=9000202, app_id=app_id, node_id=5)
    pm.set_instance_stats(there.id, {"instance_id": there.id, "cpu_percent": 30.0, "memory_mb": 100.0, "received": time.monotonic()})

    async def go():
        _MINE.set(True)
        return await stats.sample_apps([_app(app_id)], {app_id: [here, there]}, 1)
    assert _run(go()) == 2
    assert len(docker.asked_for(app_id)) == 1                                                  # Docker only for the local one
    frame = pm.get_recent_stats(app_id)[-1]
    assert frame["cpu_percent"] == 20.0 and frame["memory_mb"] == 150.0 and frame["_instance_count"] == 2   # (10+30)/2, 50+100
    assert pm.get_instance_stats(there.id)["received"]                                            # kept as its node sent it


def test_numbers_a_node_sent_too_long_ago_are_not_used(docker):
    from containers import processes as pm
    from nodes import reports
    from workers import stats
    app_id = 900021
    old = N(id=9000211, app_id=app_id, node_id=5)
    pm.set_instance_stats(old.id, {"cpu_percent": 99.0, "memory_mb": 1.0, "received": time.monotonic() - reports.MAX_AGE_SECONDS - 1})

    async def go():
        _MINE.set(True)
        return await stats.sample_apps([_app(app_id)], {app_id: [N(id=9000212, app_id=app_id, node_id=1), old]}, 1)
    _run(go())
    assert pm.get_recent_stats(app_id)[-1]["_instance_count"] == 1


def test_the_app_stream_only_gets_app_frames(client, docker):          # client: the app's event loop delivers the frames
    """The charts draw what comes over the app's stream: an instance's own numbers in between would make them jump."""
    from containers import processes as pm
    app_id = 900022
    q = pm.subscribe_stats(app_id)
    try:
        Round([(_app(app_id), _instances(app_id, 2))]).run()
        deadline = time.time() + 2                       # the frame is put on the queue by the app's own event loop
        while q.empty() and time.time() < deadline:
            time.sleep(0.02)
        time.sleep(0.1)                                  # and anything that would come right after it
        frames = []
        while not q.empty():
            frames.append(q.get_nowait())
    finally:
        pm.unsubscribe_stats(app_id, q)
    assert len(frames) == 1 and "instance_id" not in frames[0] and frames[0]["_instance_count"] == 2
