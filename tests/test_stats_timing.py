"""Stats come every 5 s, steadily: a sample does not wait for Docker's second reading, the time a round takes is not
added to the 5 s, and measuring databases on a node never holds up the stats of its instances."""
import asyncio

from containers import stats_logs


class _Container:
    id, status = "abc", "running"
    attrs = {"State": {"StartedAt": ""}}

    def __init__(self, readings):
        self.readings, self.calls = readings, []

    def stats(self, stream, one_shot=False):
        self.calls.append(one_shot)
        total, system = self.readings.pop(0)
        return {"cpu_stats": {"cpu_usage": {"total_usage": total}, "system_cpu_usage": system, "online_cpus": 2},
                "precpu_stats": {"cpu_usage": {"total_usage": total - 10}, "system_cpu_usage": system - 100},
                "memory_stats": {"usage": 0}}


def test_only_the_first_sample_waits_for_docker_and_the_cpu_is_measured_since_the_previous(monkeypatch):
    container = _Container([(1000, 10_000), (1500, 12_000)])

    class Client:
        class containers:
            @staticmethod
            def get(name):
                return container
    monkeypatch.setattr(stats_logs, "_get_client", lambda: Client)
    monkeypatch.setattr(stats_logs, "_last_cpu", {})
    first = stats_logs.get_container_stats_by_name("x")
    second = stats_logs.get_container_stats_by_name("x")
    assert container.calls == [False, True]                   # the second one is a single, quick reading
    assert first["cpu_percent"] == 20.0                        # 10 / 100 * 2 cpus, Docker's own pair
    assert second["cpu_percent"] == 50.0                       # 500 / 2000 * 2 cpus, since the previous sample


def test_slow_databases_do_not_hold_up_the_instance_stats(monkeypatch):
    from agent import report
    monkeypatch.setattr(report, "TICK_SECONDS", 0.05)
    sent = []

    async def instances(send):
        sent.append(("instances", asyncio.get_running_loop().time()))

    async def databases(db_round, send):
        await asyncio.sleep(10)                                # du on a big volume
    monkeypatch.setattr(report, "report_instances", instances)
    monkeypatch.setattr(report, "report_databases", databases)

    async def go():
        task = asyncio.create_task(report.report_forever(None))
        await asyncio.sleep(0.32)
        task.cancel()
    asyncio.run(go())
    assert len(sent) >= 3                                      # waiting for the databases would give 1


def test_the_collector_keeps_its_clock_when_a_round_is_slow(monkeypatch):
    from workers import stats
    monkeypatch.setattr(stats, "SAMPLE_SECONDS", 0.1)
    starts = []

    async def slow_round():
        starts.append(asyncio.get_running_loop().time())
        await asyncio.sleep(0.06)                              # most of the interval
    monkeypatch.setattr(stats, "collect_stats_once", slow_round)
    real_sleep = asyncio.sleep

    async def no_initial_wait(seconds):
        await real_sleep(0 if seconds == 4 else seconds)
    monkeypatch.setattr(stats.asyncio, "sleep", no_initial_wait)

    async def go():
        task = asyncio.create_task(stats._stats_collector())
        await real_sleep(0.55)
        task.cancel()
    asyncio.run(go())
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert len(gaps) >= 2 and sum(gaps) / len(gaps) < 0.14, gaps   # 0.1 on average, not 0.1 + 0.06
