"""Restarting a server through the root helper (core/power.py, scripts/cloudbase-power)."""
import sys

from core import power


def _fake(tmp_path, monkeypatch, exit_code):
    script = tmp_path / "helper.py"
    log = tmp_path / "log"
    script.write_text(chr(10).join(["import sys, pathlib",
                                 f"pathlib.Path({str(log)!r}).open('a').write(' '.join(sys.argv[1:]) + chr(10))",
                                 f"sys.exit({exit_code})"]))
    monkeypatch.setenv("CLOUDBASE_POWER_HELPER", f"{sys.executable} {script}")
    return log


def test_restart_asks_the_helper_to_reboot(tmp_path, monkeypatch):
    log = _fake(tmp_path, monkeypatch, 0)
    assert power.reboot()["status"] == "restarting"
    assert log.read_text().split() == ["reboot"]


def test_a_failing_helper_is_an_error_that_says_what_to_do(tmp_path, monkeypatch):
    _fake(tmp_path, monkeypatch, 1)
    r = power.reboot()
    assert r["status"] == "error" and "cloudbase update" in r["message"]


def test_a_missing_helper_is_an_error_too(monkeypatch):
    monkeypatch.setenv("CLOUDBASE_POWER_HELPER", "/nonexistent/cloudbase-power")
    assert power.reboot()["status"] == "error"
