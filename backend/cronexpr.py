"""Cron expressions: parse, find the next run, describe in words.

Standard five fields — minute hour day-of-month month day-of-week — with
`*`, lists (1,15), ranges (1-5), steps (*/15, 0-30/10), month and weekday
names (jan, mon) and the macros @hourly, @daily, @weekly, @monthly, @yearly.
Like classic cron, when both day-of-month and day-of-week are restricted a
day matches if either one does. Times are wall-clock times in a timezone.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MACROS = {
    "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *", "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0", "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@hourly": "0 * * * *",
}
_MONTHS = {n: i for i, n in enumerate(["jan", "feb", "mar", "apr", "may", "jun",
                                       "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_DAYS = {n: i for i, n in enumerate(["sun", "mon", "tue", "wed", "thu", "fri", "sat"])}
_DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
_MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December"]
# (name, low, high, names)
_FIELDS = [("minute", 0, 59, None), ("hour", 0, 23, None), ("day of month", 1, 31, None),
           ("month", 1, 12, _MONTHS), ("day of week", 0, 7, _DAYS)]


class CronError(ValueError):
    pass


@dataclass(frozen=True)
class Cron:
    expr: str
    minutes: frozenset
    hours: frozenset
    days: frozenset
    months: frozenset
    weekdays: frozenset   # 0 = Sunday
    any_day: bool         # day-of-month is *
    any_weekday: bool     # day-of-week is *

    def _day_matches(self, d: datetime) -> bool:
        dom = d.day in self.days
        dow = (d.isoweekday() % 7) in self.weekdays
        if self.any_day and self.any_weekday:
            return True
        if self.any_day:
            return dow
        if self.any_weekday:
            return dom
        return dom or dow

    def next_local(self, after: datetime) -> datetime:
        """First matching wall-clock time strictly after `after` (naive)."""
        t = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = after + timedelta(days=366 * 5)
        while t <= limit:
            if t.month not in self.months:
                t = (t.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
                continue
            if not self._day_matches(t):
                t = t.replace(hour=0, minute=0) + timedelta(days=1)
                continue
            if t.hour not in self.hours:
                t = t.replace(minute=0) + timedelta(hours=1)
                continue
            if t.minute not in self.minutes:
                t += timedelta(minutes=1)
                continue
            return t
        raise CronError("This schedule never runs")


def _parse_value(text: str, low: int, high: int, names: Optional[dict], field: str) -> int:
    text = text.lower()
    if names and text in names:
        return names[text]
    if not text.isdigit():
        raise CronError(f"'{text}' is not a valid {field}")
    value = int(text)
    if not low <= value <= high:
        raise CronError(f"{field.capitalize()} {value} is out of range ({low}–{high})")
    return value


def _parse_field(text: str, low: int, high: int, names: Optional[dict], field: str) -> set:
    values: set = set()
    for part in text.split(","):
        if not part:
            raise CronError(f"Empty value in the {field} field")
        rng, _, step_text = part.partition("/")
        step = 1
        if step_text:
            if not step_text.isdigit() or int(step_text) == 0:
                raise CronError(f"'/{step_text}' is not a valid step in the {field} field")
            step = int(step_text)
        if rng == "*":
            start, end = low, high
        elif "-" in rng:
            a, b = rng.split("-", 1)
            start, end = _parse_value(a, low, high, names, field), _parse_value(b, low, high, names, field)
            if start > end:
                raise CronError(f"Range {rng} in the {field} field runs backwards")
        else:
            start = _parse_value(rng, low, high, names, field)
            end = high if step_text else start
        values.update(range(start, end + 1, step))
    return values


def parse(expr: str) -> Cron:
    text = " ".join((expr or "").split())
    if not text:
        raise CronError("Enter a schedule")
    fields = MACROS.get(text.lower(), text).split(" ")
    if len(fields) != 5:
        raise CronError("A schedule has five fields: minute, hour, day of month, month and day of week")
    parsed = [_parse_field(f, low, high, names, name) for f, (name, low, high, names) in zip(fields, _FIELDS)]
    weekdays = {0 if d == 7 else d for d in parsed[4]}
    return Cron(text, frozenset(parsed[0]), frozenset(parsed[1]), frozenset(parsed[2]), frozenset(parsed[3]),
                frozenset(weekdays), fields[2] == "*", fields[4] == "*")


def zone(name: Optional[str]):
    if not name or name == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise CronError(f"Unknown timezone '{name}'")


def _matches(cron: Cron, local: datetime) -> bool:
    return (local.minute in cron.minutes and local.hour in cron.hours
            and local.month in cron.months and cron._day_matches(local))


def next_run(cron: Cron, after_utc: datetime, tz_name: Optional[str] = None) -> datetime:
    """Next run strictly after `after_utc` (naive UTC), as naive UTC.

    Jobs for every hour step through real minutes, so they keep running
    through the hour a DST change repeats or skips. Jobs at set hours go by
    the wall clock, so "02:30 daily" runs exactly once on those days too.
    """
    tz = zone(tz_name)
    if len(cron.hours) == 24:
        t = after_utc.replace(second=0, microsecond=0)
        for _ in range(60 * 24 * 8):
            t += timedelta(minutes=1)
            if _matches(cron, t.replace(tzinfo=timezone.utc).astimezone(tz)):
                return t
    local = after_utc.replace(tzinfo=timezone.utc).astimezone(tz).replace(tzinfo=None)
    while True:
        candidate = cron.next_local(local)
        utc = candidate.replace(tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
        if utc > after_utc:
            return utc
        local = candidate  # a wall-clock time repeated by a DST change: take the next one


def next_runs(cron: Cron, after_utc: datetime, tz_name: Optional[str], count: int) -> list[datetime]:
    runs, t = [], after_utc
    for _ in range(count):
        t = next_run(cron, t, tz_name)
        runs.append(t)
    return runs


def _list(values: list, fmt=str) -> str:
    items = [fmt(v) for v in values]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def describe(cron: Cron) -> str:
    """The schedule in words, for the common shapes; otherwise the expression."""
    mins, hours = sorted(cron.minutes), sorted(cron.hours)
    all_days = cron.any_day and cron.any_weekday and len(cron.months) == 12

    def step_of(values: list, size: int) -> Optional[int]:
        if len(values) < 2 or values[0] != 0:
            return None
        step = values[1] - values[0]
        # */7 runs at :56 and then :00 — only call it "every 7" when the steps divide evenly
        return step if size % step == 0 and values == list(range(0, size, step)) else None

    if len(mins) == 60 and len(hours) == 24 and all_days:
        return "Every minute"
    if len(hours) == 24 and all_days and (s := step_of(mins, 60)):
        return f"Every {s} minutes"
    if len(hours) == 24 and all_days and len(mins) == 1:
        return "Every hour" + (f" at :{mins[0]:02d}" if mins[0] else "")
    if len(mins) == 1 and all_days and (s := step_of(hours, 24)):
        return f"Every {s} hours" + (f" at :{mins[0]:02d}" if mins[0] else "")

    if len(mins) == 1 and len(hours) <= 4:
        at = _list([f"{h:02d}:{mins[0]:02d}" for h in hours])
        if all_days:
            return f"Every day at {at}"
        if cron.any_day and len(cron.months) == 12:
            days = sorted(cron.weekdays)
            if days == [1, 2, 3, 4, 5]:
                return f"Weekdays at {at}"
            if days == [0, 6]:
                return f"Weekends at {at}"
            return f"Every {_list(days, lambda d: _DAY_NAMES[d])} at {at}"
        if cron.any_weekday and len(cron.months) == 12:
            days = sorted(cron.days)
            return f"On day {_list(days)} of every month at {at}"
        if cron.any_weekday and len(cron.months) == 1 and len(cron.days) == 1:
            return f"Every year on {_MONTH_NAMES[next(iter(cron.months))]} {next(iter(cron.days))} at {at}"

    # Every N minutes within a window of hours and/or on certain days
    every = "Every minute" if len(mins) == 60 else (f"Every {s} minutes" if (s := step_of(mins, 60)) else None)
    contiguous = hours == list(range(hours[0], hours[-1] + 1))
    if every and contiguous and len(cron.months) == 12 and (cron.any_day or cron.any_weekday):
        parts = [every]
        if len(hours) < 24:
            parts.append(f"from {hours[0]:02d}:00 to {hours[-1]:02d}:59")
        if not cron.any_weekday:
            days = sorted(cron.weekdays)
            parts.append("on weekdays" if days == [1, 2, 3, 4, 5] else "on weekends" if days == [0, 6]
                         else "on " + _list(days, lambda d: _DAY_NAMES[d] + "s"))
        elif not cron.any_day:
            parts.append(f"on day {_list(sorted(cron.days))} of the month")
        return " ".join(parts)
    return f"Custom: {cron.expr}"
