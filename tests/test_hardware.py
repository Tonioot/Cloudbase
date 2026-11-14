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


def test_a_raspberry_pi_5_adds_up_its_supply_rails(monkeypatch):
    out = chr(10).join(["  3V7_WL_SW_A current(0)=0.01000000A", "  VDD_CORE_A current(7)=2.00000000A",
                     "  3V7_WL_SW_V volt(8)=3.70000000V", "  VDD_CORE_V volt(15)=0.90000000V", "  EXT5V_V volt(24)=4.90000000V"])
    monkeypatch.setattr(hardware, "_vcgencmd", lambda *a: out)
    assert hardware._pmic_watts() == round(0.01 * 3.7 + 2.0 * 0.9, 2)


def test_consumption_prefers_the_whole_board_over_the_processor(monkeypatch):
    monkeypatch.setattr(hardware, "_pmic_watts", lambda: None)
    monkeypatch.setattr(hardware, "_hwmon_watts", lambda: None)
    monkeypatch.setattr(hardware, "_battery_watts", lambda: 12.5)
    monkeypatch.setattr(hardware, "_rapl_watts", lambda: 40.0)
    assert hardware.watts() == {"watts": 12.5, "source": "battery"}


def test_without_any_way_to_measure_there_is_no_consumption(monkeypatch):
    for name in ("_pmic_watts", "_hwmon_watts", "_battery_watts", "_rapl_watts"):
        monkeypatch.setattr(hardware, name, lambda: None)
    assert hardware.watts() is None
