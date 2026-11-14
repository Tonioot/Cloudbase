"""What a server says about its own health (core/hardware.py)."""
from core import hardware


def test_a_raspberry_pi_reports_which_power_problems_it_has_now_and_had_since_boot(monkeypatch):
    monkeypatch.setattr(hardware, "throttled_value", lambda: 0x50005)     # undervoltage + throttled, now and since boot
    power = hardware.power()
    assert power["now"] == ["undervoltage", "throttled"] and power["since_boot"] == ["undervoltage", "throttled"]


def test_a_problem_that_is_gone_shows_only_since_boot(monkeypatch):
    monkeypatch.setattr(hardware, "throttled_value", lambda: 0x10000)
    power = hardware.power()
    assert power["now"] == [] and power["since_boot"] == ["undervoltage"]


def test_hardware_without_a_power_report_has_none(monkeypatch):
    monkeypatch.setattr(hardware, "throttled_value", lambda: None)
    monkeypatch.setattr(hardware.psutil, "sensors_battery", lambda: None, raising=False)
    assert hardware.power() is None


def test_collect_never_raises_and_leaves_out_what_the_machine_does_not_have(monkeypatch):
    def broken():
        raise OSError("no sensor")
    monkeypatch.setattr(hardware, "temperature", broken)
    out = hardware.collect()
    assert "temperature_c" not in out and isinstance(out, dict)
