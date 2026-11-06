from datetime import datetime

import pytest

from cron import expr as cx


def _next(expr, after, tz=None):
    return cx.next_run(cx.parse(expr), after, tz)


def test_every_15_minutes():
    assert _next("*/15 * * * *", datetime(2026, 1, 1, 10, 7)) == datetime(2026, 1, 1, 10, 15)
    assert _next("*/15 * * * *", datetime(2026, 1, 1, 10, 45)) == datetime(2026, 1, 1, 11, 0)


def test_next_run_is_strictly_after():
    assert _next("0 9 * * *", datetime(2026, 1, 1, 9, 0)) == datetime(2026, 1, 2, 9, 0)


def test_macros():
    assert _next("@daily", datetime(2026, 1, 1, 10, 0)) == datetime(2026, 1, 2, 0, 0)
    assert _next("@hourly", datetime(2026, 1, 1, 10, 30)) == datetime(2026, 1, 1, 11, 0)


def test_weekday_names_and_sunday_as_7():
    # 2026-01-05 is a Monday
    assert _next("0 9 * * mon", datetime(2026, 1, 1)) == datetime(2026, 1, 5, 9, 0)
    assert cx.parse("0 0 * * 7").weekdays == cx.parse("0 0 * * 0").weekdays == frozenset({0})


def test_day_of_month_or_weekday_matches_either():
    cron = cx.parse("0 0 13 * fri")
    assert cron._day_matches(datetime(2026, 2, 13))   # the 13th (a Friday) and
    assert cron._day_matches(datetime(2026, 2, 6))    # any Friday
    assert not cron._day_matches(datetime(2026, 2, 7))


def test_timezone_is_wall_clock():
    # 09:00 in Amsterdam is 08:00 UTC in winter and 07:00 UTC in summer
    assert _next("0 9 * * *", datetime(2026, 1, 10, 0, 0), "Europe/Amsterdam") == datetime(2026, 1, 10, 8, 0)
    assert _next("0 9 * * *", datetime(2026, 7, 10, 0, 0), "Europe/Amsterdam") == datetime(2026, 7, 10, 7, 0)


def test_daily_job_runs_once_on_dst_days():
    runs = cx.next_runs(cx.parse("30 2 * * *"), datetime(2026, 3, 27), "Europe/Amsterdam", 5)
    assert len({r.date() for r in runs}) == 5  # never twice on one day, never skipped


@pytest.mark.parametrize("expr", ["", "   ", "* * * *", "* * * * * *", "61 * * * *", "* 24 * * *",
                                  "* * 0 * *", "* * * 13 *", "abc * * * *", "*/0 * * * *", "5-1 * * * *"])
def test_invalid_expressions_are_rejected(expr):
    with pytest.raises(cx.CronError):
        cx.parse(expr)


def test_impossible_schedule_is_reported_not_looped_forever():
    with pytest.raises(cx.CronError):
        _next("0 0 31 2 *", datetime(2026, 1, 1))


def test_unknown_timezone_is_rejected():
    with pytest.raises(cx.CronError):
        cx.zone("Mars/Olympus")


def test_describe_common_shapes():
    assert "15 minutes" in cx.describe(cx.parse("*/15 * * * *"))
    assert cx.describe(cx.parse("5 4 1 1 *")) != ""
