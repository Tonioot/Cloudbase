"""Writing nginx configs costs a `nginx -t` (which reads every config on the server) and a reload each time.

So a config that did not change is not written, tested or reloaded again, and a batch of changes (one per app,
after the base domain changed) is tested and reloaded once.
"""
import asyncio
import os

import pytest

from nginx import files


class FakeSystem:
    """Stands in for sudo, nginx and systemctl, on a throwaway folder; counts what was run."""

    def __init__(self, tmp_path):
        self.sites = tmp_path / "sites-available"
        self.enabled = tmp_path / "sites-enabled"
        self.maint = tmp_path / "maintenance"
        for d in (self.sites, self.enabled, self.maint):
            d.mkdir()
        self.calls = []
        self.nginx_accepts = True

    def count(self, *words):
        return sum(1 for c in self.calls if all(w in c for w in words))

    def run(self, cmd, input=None, **kwargs):
        assert cmd[0] == "sudo", cmd
        self.calls.append(tuple(cmd[1:]))
        rc, out, err = 0, "", ""
        if cmd[1] == "tee":
            with open(cmd[2], "w", encoding="utf-8") as f:
                f.write(input)
        elif cmd[1] == "ln":
            open(cmd[4], "w").close()                      # a symlink is a file here: it exists or it does not
        elif cmd[1] == "rm":
            try:
                os.remove(cmd[3])
            except OSError:
                pass
        elif cmd[1] == "mkdir":
            os.makedirs(cmd[3], exist_ok=True)
        elif cmd[1] == "nginx" and not self.nginx_accepts:
            rc, err = 1, "nginx: configuration file test failed"

        class Result:
            returncode, stdout, stderr = rc, out, err
        return Result


@pytest.fixture
def system(tmp_path, monkeypatch):
    fake = FakeSystem(tmp_path)
    monkeypatch.setattr(files.subprocess, "run", fake.run)
    monkeypatch.setattr(files, "NGINX_SITES_DIR", str(fake.sites))
    monkeypatch.setattr(files, "NGINX_ENABLED_DIR", str(fake.enabled))
    monkeypatch.setattr(files, "MAINTENANCE_DIR", str(fake.maint))
    monkeypatch.setattr(files, "_failed", set())
    monkeypatch.setattr(files, "_batch_depth", 0)
    monkeypatch.setattr(files, "_batch_pending", set())
    return fake


def test_a_config_is_written_tested_and_reloaded(system):
    assert files.write_nginx_config("app-1", "server { a }") == (True, "OK")
    assert (system.count("tee"), system.count("nginx", "-t"), system.count("reload")) == (1, 1, 1)


def test_the_same_config_again_runs_nothing_at_all(system):
    files.write_nginx_config("app-1", "server { a }")
    before = len(system.calls)
    assert files.write_nginx_config("app-1", "server { a }") == (True, "unchanged")
    assert len(system.calls) == before


def test_a_changed_config_is_written_again(system):
    files.write_nginx_config("app-1", "server { a }")
    files.write_nginx_config("app-1", "server { b }")
    assert (system.count("tee"), system.count("nginx", "-t"), system.count("reload")) == (2, 2, 2)
    assert (system.sites / "app-1").read_text() == "server { b }"


def test_a_config_nginx_refused_is_not_taken_for_unchanged(system):
    system.nginx_accepts = False
    ok, _ = files.write_nginx_config("app-1", "server { broken }")
    assert not ok
    system.nginx_accepts = True
    # The file on disk is the same, but it was never accepted: it is tested again, and now it goes through
    assert files.write_nginx_config("app-1", "server { broken }") == (True, "OK")
    assert files.write_nginx_config("app-1", "server { broken }") == (True, "unchanged")


def test_a_batch_tests_and_reloads_once(system):
    files.begin_batch()
    for i in range(5):
        assert files.write_nginx_config(f"app-{i}", f"server {{ {i} }}") == (True, "queued")
    assert (system.count("tee"), system.count("nginx", "-t"), system.count("reload")) == (5, 0, 0)
    assert files.end_batch() == (True, "OK")
    assert (system.count("nginx", "-t"), system.count("reload")) == (1, 1)


def test_a_batch_that_nginx_refuses_reports_it_and_retests_each_config_later(system):
    files.begin_batch()
    files.write_nginx_config("app-1", "server { x }")
    system.nginx_accepts = False
    ok, msg = files.end_batch()
    assert not ok and "failed" in msg
    system.nginx_accepts = True
    assert files.write_nginx_config("app-1", "server { x }") == (True, "OK")


def test_an_empty_batch_runs_nothing(system):
    files.begin_batch()
    files.write_nginx_config("app-1", "server { a }")
    files.end_batch()
    calls = len(system.calls)
    files.begin_batch()
    files.write_nginx_config("app-1", "server { a }")      # unchanged: nothing pending
    assert files.end_batch() == (True, "OK")
    assert len(system.calls) == calls


def test_nested_batches_finish_with_the_outer_one(system):
    files.begin_batch()
    files.begin_batch()
    files.write_nginx_config("app-1", "server { a }")
    files.end_batch()
    assert system.count("reload") == 0
    files.end_batch()
    assert system.count("reload") == 1


def test_removing_a_config_that_is_not_there_reloads_nothing(system):
    assert files.remove_nginx_config("app-9") is False
    assert system.calls == []


def test_removing_a_config_reloads_once_or_waits_for_the_batch(system):
    files.write_nginx_config("app-1", "server { a }")
    files.write_nginx_config("app-2", "server { b }")
    system.calls.clear()
    assert files.remove_nginx_config("app-1") is True
    assert system.count("reload") == 1
    files.begin_batch()
    assert files.remove_nginx_config("app-2") is True
    assert system.count("reload") == 1
    files.end_batch()
    assert system.count("reload") == 2


def test_maintenance_pages_are_only_written_when_they_changed(system):
    pages = dict(downtime_html="d", update_html="u", restart_html="r", starting_html="s")
    assert files.write_maintenance_files(1, **pages) == (True, "OK")
    assert system.count("tee") == 4
    system.calls.clear()
    assert files.write_maintenance_files(1, **pages) == (True, "unchanged")
    assert system.calls == []
    assert files.write_maintenance_files(1, **{**pages, "update_html": "u2"}) == (True, "OK")
    assert system.count("tee") == 1 and system.count("chmod") == 1


def test_refreshing_every_app_is_one_batch(monkeypatch):
    from nginx import panel
    order = []
    monkeypatch.setattr(panel.nginx_files, "begin_batch", lambda: order.append("begin"))
    monkeypatch.setattr(panel.nginx_files, "end_batch", lambda: order.append("end") or (True, "OK"))

    async def refresh():
        await panel._refresh_all_app_nginx()
    asyncio.run(refresh())
    assert order == ["begin", "end"]
