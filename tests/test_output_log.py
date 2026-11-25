"""An app's output is copied to disk per app and per day, can be searched afterwards, and is pruned by age and size."""
import datetime as dt

import pytest

from containers import output_log


class FakeContainer:
    """What output_log uses of a Docker container: labels and logs(stdout/stderr, timestamps, since/tail)."""

    def __init__(self, cid="c1", app_id=7, instance_id=3):
        self.id, self.name = cid, f"cloudbase-app-{app_id}-instance-{instance_id}"
        self.labels = {"cloudbase.app_id": str(app_id), "cloudbase.instance_id": str(instance_id)}
        self.out, self.err = [], []
        self.asked = []

    def print(self, ts, text, stderr=False):
        (self.err if stderr else self.out).append(f"{ts} {text}")

    def logs(self, stdout=True, stderr=True, timestamps=True, since=None, tail=None):
        self.asked.append({"since": since, "tail": tail})
        lines = (self.out if stdout else []) + (self.err if stderr else [])
        if since is not None:
            lines = [line for line in lines if output_log.to_ns(line.split(" ")[0]) >= since * 1_000_000_000]
        return ("\n".join(lines) + ("\n" if lines else "")).encode()


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(output_log, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(output_log, "_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(output_log, "_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(output_log, "_state", None)
    return tmp_path


def ns(text):
    return output_log.to_ns(text)


def test_timestamps_keep_their_nanoseconds():
    assert ns("1970-01-01T00:00:01.5Z") == 1_500_000_000
    assert ns("1970-01-01T00:00:01.000000007Z") == 1_000_000_007
    assert ns("1970-01-01T00:00:02Z") == 2_000_000_000
    assert ns("not a time") is None


def test_output_is_copied_once_per_line_into_a_file_per_day(isolated):
    c = FakeContainer()
    c.print("2026-11-18T23:59:59.100000000Z", "late")
    c.print("2026-11-19T00:00:01.200000000Z", "boom", stderr=True)
    assert output_log.capture(c) == 2
    assert c.asked[0]["tail"] == output_log.FIRST_CAPTURE_TAIL       # a new container: only its last lines
    c.print("2026-11-19T00:00:02.000000000Z", "again")
    assert output_log.capture(c) == 1                                 # only what is new
    assert output_log.capture(c) == 0
    assert c.asked[-1]["since"] == ns("2026-11-19T00:00:02Z") // 1_000_000_000

    day1 = (isolated / "app-7" / "2026-11-18.log").read_text(encoding="utf-8").splitlines()
    day2 = (isolated / "app-7" / "2026-11-19.log").read_text(encoding="utf-8").splitlines()
    assert day1 == ["2026-11-18T23:59:59.100000000Z\t3\to\tlate"]
    assert day2 == ["2026-11-19T00:00:01.200000000Z\t3\te\tboom", "2026-11-19T00:00:02.000000000Z\t3\to\tagain"]


def test_containers_without_cloudbase_labels_are_left_alone():
    c = FakeContainer()
    c.labels = {}
    c.print("2026-11-19T00:00:00Z", "x")
    assert output_log.capture(c) == 0


def test_search_finds_the_newest_matching_lines_first():
    c = FakeContainer()
    for minute in range(10):
        c.print(f"2026-11-19T10:{minute:02d}:00Z", f"request {minute}")
    c.print("2026-11-19T10:05:30Z", "ERROR database unreachable", stderr=True)
    other = FakeContainer(cid="c2", instance_id=4)
    other.print("2026-11-19T10:07:30Z", "Traceback (most recent call last)")
    output_log.capture(c)
    output_log.capture(other)
    since = ns("2026-11-19T00:00:00Z")

    newest = output_log.search(7, since_ns=since, limit=3)
    assert [e["text"] for e in newest] == ["request 9", "request 8", "Traceback (most recent call last)"]

    errors = output_log.search(7, since_ns=since, errors_only=True)
    assert [e["text"] for e in errors] == ["Traceback (most recent call last)", "ERROR database unreachable"]
    assert errors[1]["stream"] == "stderr" and errors[1]["instance_id"] == 3

    assert [e["text"] for e in output_log.search(7, since_ns=since, q="REQUEST 4")] == ["request 4"]
    assert len(output_log.search(7, since_ns=since, instance_id=4)) == 1
    older = output_log.search(7, since_ns=since, until_ns=ns("2026-11-19T10:02:00Z"))
    assert [e["text"] for e in older] == ["request 1", "request 0"]
    assert output_log.search(7, since_ns=ns("2026-11-20T00:00:00Z")) == []
    assert output_log.search(99, since_ns=since) == []


def test_old_days_and_too_much_output_are_pruned(isolated):
    folder = isolated / "app-7"
    folder.mkdir()
    for day in ("2026-11-01", "2026-11-17", "2026-11-18", "2026-11-19"):
        (folder / f"{day}.log").write_text("x" * (600 * 1024), encoding="utf-8")
    today = dt.date(2026, 11, 19)

    assert output_log.prune(retention_days=5, max_mb_per_app=100, today=today) == 1          # 1 November is too old
    assert sorted(p.name for p in folder.iterdir()) == ["2026-11-17.log", "2026-11-18.log", "2026-11-19.log"]

    assert output_log.prune(retention_days=30, max_mb_per_app=1, today=today) == 2           # 1.8 MB > 1 MB
    assert [p.name for p in folder.iterdir()] == ["2026-11-19.log"]                          # today always stays


def test_a_deleted_app_loses_its_output(isolated):
    c = FakeContainer()
    c.print("2026-11-19T00:00:00Z", "x")
    output_log.capture(c)
    output_log.forget_app(7)
    assert not (isolated / "app-7").exists()


def test_settings_from_the_primary_are_kept_and_checked():
    assert output_log.settings() == output_log.DEFAULT_SETTINGS
    output_log.set_settings({"retention_days": 30, "unknown": 1, "max_mb_per_app": "lots"})
    assert output_log.settings() == {"retention_days": 30, "max_mb_per_app": 200}


def test_a_build_on_this_machine_is_kept_with_the_apps_output():
    # a node builds the image while an instance starts: whoever follows the app's output sees the build too
    output_log.add_build_line(7, "[Docker build] Collecting flask")
    output_log.add_build_line(7, "[Docker build] ERROR:\tno matching distribution\nfor flask==9")
    found = output_log.search(7, since_ns=0)
    assert [(e["stream"], e["instance_id"]) for e in found] == [("build", None), ("build", None)]
    assert found[0]["text"] == "[Docker build] ERROR: no matching distribution for flask==9"      # one line, no tabs
    assert [e["text"] for e in output_log.search(7, since_ns=0, errors_only=True)] == [found[0]["text"]]
    assert output_log.search(7, since_ns=0, instance_id=3) == []          # not an instance's output
