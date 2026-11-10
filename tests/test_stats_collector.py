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
    return [N(id=app_id * 1000 + i, app_id=app_id) for i in range(count)]


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
    monkeypatch.setattr(pm, "_watched_until", {})
    monkeypatch.setattr(pm, "_stats_queues", {})
    return fake


class Round:
    """A collector with its own memory of when it last looked at each app."""

    def __init__(self, apps_and_instances):
        self.apps = [a for a, _ in apps_and_instances]
        self.instances = {a.id: r for a, r in apps_and_instances}
        self.last = {}

    def at(self, now):
        from workers import stats

        async def go():
            _MINE.set(True)
            return await stats.sample_due_apps(self.apps, self.instances, None, now, self.last)
        return _run(go())


def test_every_instance_is_asked_once_per_round(docker):
    from containers import processes as pm
    app_id = 900001
    instances = _instances(app_id, 3)
    assert Round([(_app(app_id), instances)]).at(1000.0) == 3
    assert len(docker.asked_for(app_id)) == 3                             # not twice each, as it used to be
    assert all(pm.get_instance_stats(r.id)["cpu_percent"] == 10.0 for r in instances)
    frames = pm.get_recent_stats(app_id)
    assert len(frames) == 1 and frames[0]["memory_mb"] == 150.0 and frames[0]["cpu_percent"] == 10.0


def test_an_app_nobody_watches_is_sampled_every_15_seconds(docker):
    app_id = 900002
    rounds = Round([(_app(app_id), _instances(app_id, 2))])
    rounds.at(1000.0)
    rounds.at(1002.0)
    rounds.at(1010.0)
    assert len(docker.asked_for(app_id)) == 2                             # one round so far
    rounds.at(1015.0)
    assert len(docker.asked_for(app_id)) == 4


def test_an_app_somebody_watches_is_sampled_every_round(docker):
    from containers import processes as pm
    app_id = 900003
    rounds = Round([(_app(app_id), _instances(app_id, 2))])
    rounds.at(1000.0)
    pm.mark_watched(app_id, seconds=60)                                   # the instances tab asked for numbers
    rounds.at(time.monotonic())
    rounds.at(time.monotonic() + 1)
    assert len(docker.asked_for(app_id)) == 6


def test_an_open_stats_tab_counts_as_watching(docker):
    from containers import processes as pm
    app_id = 900004
    rounds = Round([(_app(app_id), _instances(app_id, 1))])
    queue = pm.subscribe_stats(app_id)
    rounds.at(1000.0)
    rounds.at(1002.0)
    assert len(docker.asked_for(app_id)) == 2
    pm.unsubscribe_stats(app_id, queue)
    rounds.at(1004.0)
    assert len(docker.asked_for(app_id)) == 2                             # the tab closed: back to every 15 s


def test_the_instances_tab_marks_the_app_as_watched(admin):
    from containers import processes as pm
    from core.database import AsyncSessionLocal
    from core.models import Application

    async def make():
        async with AsyncSessionLocal() as db:
            app = Application(name="collect-marks", repo_url="https://example.test/x", start_command="x", port=3000)
            db.add(app)
            await db.commit()
            return app.id
    app_id = _run(make())
    assert not pm.is_watched(app_id)
    assert admin.get(f"/api/apps/{app_id}/instances/stats").status_code == 200
    assert pm.is_watched(app_id)
    assert not pm.is_watched(app_id, now=time.monotonic() + 60)           # and it lapses when the tab is gone


def test_apps_on_other_nodes_are_left_to_their_own_node(docker):
    from workers import stats
    app_id = 900005
    remote = _app(app_id, node_id=77)
    last = {}

    async def go():
        _MINE.set(True)
        return await stats.sample_due_apps([remote], {app_id: _instances(app_id, 2)}, 1, 1000.0, last)
    assert _run(go()) == 0 and docker.asked_for(app_id) == []


def test_an_app_without_running_instances_here_is_not_asked_about(docker):
    app_id = 900006
    assert Round([(_app(app_id), [])]).at(1000.0) == 0
    assert docker.asked_for(app_id) == []


def test_many_instances_never_run_more_stats_calls_at_once_than_the_limit(docker):
    from workers import stats
    app_id = 900007
    assert Round([(_app(app_id), _instances(app_id, 40))]).at(1000.0) == 40
    assert len(docker.asked_for(app_id)) == 40
    assert docker.busiest <= stats.STATS_CONCURRENCY


def test_apps_that_stopped_running_here_are_forgotten():
    from workers import stats
    last = {900008: 1.0, 900009: 2.0}

    async def go():
        return await stats.sample_due_apps([], {900009: []}, None, 1000.0, last)
    _run(go())
    assert last == {900009: 2.0}


def test_the_poller_no_longer_samples_local_instances_a_third_time():
    import inspect
    from workers import stats
    assert "get_container_stats_by_name" not in inspect.getsource(stats._remote_instance_stats_poller)
