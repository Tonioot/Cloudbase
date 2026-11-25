"""What an app's containers print, kept on disk on the machine that runs them, so it can be searched later.

Docker only keeps a container's output as long as the container exists; every deploy and restart removes
containers, and with them the output that explains a crash. The primary and every node's agent therefore copy the
output of their app containers to

    ~/.cloudbase/logs/output/app-<app_id>/<YYYY-MM-DD>.log      one line per output line:
    <timestamp>\t<instance id>\t<o|e (stdout/stderr)>\t<text>

every few seconds, and once more just before a container is removed (see instances.py). The output of an image build
made while an instance starts (a node builds the image itself) is kept there too, as stream b without an instance id:
whoever follows the app's output sees its build as well, also when it runs on a node. Each machine prunes its
own files with the settings the primary hands out (days to keep, a maximum per app), and answers searches over them
(routers/log_history.py asks the nodes through the agent).
"""
import asyncio
import collections
import datetime as _dt
import json
import logging
import os
import re
import shutil
import threading
import time
from typing import Optional

from containers.client import _get_client, is_docker_available
from core.system_logs import LOG_DIR

log = logging.getLogger("cloudbase.output_log")

OUTPUT_DIR = os.path.join(LOG_DIR, "output")
_STATE_FILE = os.path.join(OUTPUT_DIR, "state.json")        # container id -> last copied timestamp (ns)
_SETTINGS_FILE = os.path.join(OUTPUT_DIR, "settings.json")  # what the primary handed out

DEFAULT_SETTINGS = {"retention_days": 14, "max_mb_per_app": 200}
CAPTURE_EVERY_SECONDS = 10
PRUNE_EVERY_SECONDS = 3600
FIRST_CAPTURE_TAIL = 5000          # a container seen for the first time: its last lines, not everything Docker kept

# Lines that read as a problem, for "errors only"
ERROR_RE = re.compile(r"\b(error|err|exception|fatal|panic|traceback|critical|crit|failed|failure)\b|level=(error|fatal)",
                      re.IGNORECASE)

_lock = threading.Lock()
_state: Optional[dict[str, int]] = None


# ── Timestamps ────────────────────────────────────────────────────────────────

def to_ns(ts: str) -> Optional[int]:
    """Docker's RFC 3339 timestamp ("2026-11-19T10:00:00.123456789Z") as nanoseconds since the epoch."""
    try:
        date, _, frac = ts.rstrip("Z").partition(".")
        dt = _dt.datetime.strptime(date, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=_dt.timezone.utc)
        return int(dt.timestamp()) * 1_000_000_000 + int((frac + "000000000")[:9] or 0)
    except ValueError:
        return None


def _day_of(ns: int) -> str:
    return _dt.datetime.fromtimestamp(ns / 1e9, _dt.timezone.utc).strftime("%Y-%m-%d")


def _app_dir(app_id: int) -> str:
    return os.path.join(OUTPUT_DIR, f"app-{int(app_id)}")


# ── State and settings ────────────────────────────────────────────────────────

def _load_state() -> dict[str, int]:
    global _state
    if _state is None:
        try:
            with open(_STATE_FILE, encoding="utf-8") as f:
                _state = {k: int(v) for k, v in json.load(f).items()}
        except (OSError, ValueError):
            _state = {}
    return _state


def _save_state() -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    part = _STATE_FILE + ".part"
    with open(part, "w", encoding="utf-8") as f:
        json.dump(_load_state(), f)
    os.replace(part, _STATE_FILE)


def settings() -> dict:
    try:
        with open(_SETTINGS_FILE, encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        saved = {}
    return {**DEFAULT_SETTINGS, **{k: saved[k] for k in DEFAULT_SETTINGS if isinstance(saved.get(k), int)}}


def set_settings(values: dict) -> dict:
    """Keep the settings the primary handed out; they stay in force when the primary is away."""
    clean = {k: int(values[k]) for k in DEFAULT_SETTINGS if isinstance(values.get(k), (int, float))}
    merged = {**settings(), **clean}
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f)
    return merged


# ── Copying output ────────────────────────────────────────────────────────────

def _read(container, since_s: Optional[int]) -> list[tuple[int, str, str]]:
    """(ns, stream, text) of the container's output, from `since_s` on (or its last lines)."""
    out = []
    for stream, kw in (("o", {"stdout": True, "stderr": False}), ("e", {"stdout": False, "stderr": True})):
        extra = {"since": since_s} if since_s is not None else {"tail": FIRST_CAPTURE_TAIL}
        raw = container.logs(timestamps=True, **kw, **extra)
        for line in raw.decode("utf-8", errors="replace").splitlines():
            ts, _, text = line.partition(" ")
            ns = to_ns(ts)
            if ns is not None:
                out.append((ns, stream, text.replace("\r", "")))
    out.sort(key=lambda e: e[0])
    return out


def capture(container) -> int:
    """Copy what this container printed since the last copy. Returns the number of new lines."""
    labels = getattr(container, "labels", None) or {}
    app_id, instance_id = labels.get("cloudbase.app_id"), labels.get("cloudbase.instance_id")
    if not app_id or not instance_id:
        return 0
    with _lock:
        state = _load_state()
        last = state.get(container.id)
        try:
            entries = _read(container, last // 1_000_000_000 if last else None)
        except Exception as e:                       # gone in the meantime, or Docker busy: next round
            log.debug("[output] could not read %s: %s", getattr(container, "name", container.id), e)
            return 0
        new = [e for e in entries if last is None or e[0] > last]
        if not new:
            return 0
        folder = _app_dir(int(app_id))
        os.makedirs(folder, exist_ok=True)
        by_day: dict[str, list[str]] = collections.defaultdict(list)
        for ns, stream, text in new:
            stamp = _dt.datetime.fromtimestamp(ns // 1_000_000_000, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            by_day[_day_of(ns)].append(f"{stamp}.{ns % 1_000_000_000:09d}Z\t{instance_id}\t{stream}\t{text}\n")
        for day, lines in by_day.items():
            with open(os.path.join(folder, f"{day}.log"), "a", encoding="utf-8") as f:
                f.writelines(lines)
        state[container.id] = new[-1][0]
        return len(new)


def add_build_line(app_id: int, text: str) -> None:
    """One line of an image build of this app made on this machine (stream b). Never stands in the way of the build."""
    try:
        ns = time.time_ns()
        stamp = _dt.datetime.fromtimestamp(ns // 1_000_000_000, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        text = str(text).replace("\t", " ").replace("\n", " ").replace("\r", "")
        folder = _app_dir(int(app_id))
        with _lock:
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, f"{_day_of(ns)}.log"), "a", encoding="utf-8") as f:
                f.write(f"{stamp}.{ns % 1_000_000_000:09d}Z\t\tb\t{text}\n")
    except Exception as e:
        log.debug("[output] build line not kept: %s", e)


def capture_quietly(container) -> None:
    """The last lines of a container about to be removed (never stands in the way of removing it)."""
    try:
        capture(container)
        with _lock:
            _save_state()
    except Exception as e:
        log.debug("[output] final copy failed: %s", e)


def capture_all(client) -> int:
    """One round over every app container on this machine; forgets containers that are gone."""
    total, seen = 0, set()
    for container in client.containers.list(all=True, filters={"label": "cloudbase.app_id"}):
        seen.add(container.id)
        total += capture(container)
    with _lock:
        state = _load_state()
        for gone in [cid for cid in state if cid not in seen]:
            del state[gone]
        _save_state()
    return total


# ── Pruning ───────────────────────────────────────────────────────────────────

def prune(retention_days: int, max_mb_per_app: int, today: Optional[_dt.date] = None) -> int:
    """Remove days older than the retention, then the oldest days of an app over its maximum (never today).
    Returns the number of files removed."""
    if not os.path.isdir(OUTPUT_DIR):
        return 0
    today = today or _dt.datetime.now(_dt.timezone.utc).date()
    oldest_kept = (today - _dt.timedelta(days=max(1, retention_days) - 1)).isoformat()
    removed = 0
    for name in os.listdir(OUTPUT_DIR):
        folder = os.path.join(OUTPUT_DIR, name)
        if not (name.startswith("app-") and os.path.isdir(folder)):
            continue
        days = sorted(f for f in os.listdir(folder) if f.endswith(".log"))
        for f in [f for f in days if f[:-4] < oldest_kept]:
            os.remove(os.path.join(folder, f))
            removed += 1
        days = sorted(f for f in os.listdir(folder) if f.endswith(".log"))
        sizes = {f: os.path.getsize(os.path.join(folder, f)) for f in days}
        total, limit = sum(sizes.values()), max(1, max_mb_per_app) * 1024 * 1024
        for f in days[:-1]:                         # oldest first, today's file stays
            if total <= limit:
                break
            os.remove(os.path.join(folder, f))
            total -= sizes[f]
            removed += 1
        if not os.listdir(folder):
            shutil.rmtree(folder, ignore_errors=True)
    return removed


def forget_app(app_id: int) -> None:
    """A deleted app's output goes with it."""
    shutil.rmtree(_app_dir(app_id), ignore_errors=True)


# ── Searching ─────────────────────────────────────────────────────────────────

def search(app_id: int, *, since_ns: int, until_ns: Optional[int] = None, q: str = "", errors_only: bool = False,
           instance_id: Optional[int] = None, limit: int = 500) -> list[dict]:
    """The newest `limit` lines of an app's output between since and until (exclusive) that match, newest first.
    Reads day by day from the newest, keeping only the last matches of each file in memory."""
    folder = _app_dir(app_id)
    if not os.path.isdir(folder):
        return []
    until_ns = until_ns or (1 << 63)
    first_day, last_day = _day_of(since_ns), _day_of(min(until_ns, 4_102_444_800 * 10**9))
    needle = q.lower().strip()
    found: list[dict] = []
    for f in sorted((f for f in os.listdir(folder) if f.endswith(".log")), reverse=True):
        if not (first_day <= f[:-4] <= last_day):
            continue
        keep: collections.deque = collections.deque(maxlen=limit - len(found))
        with open(os.path.join(folder, f), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                ts, inst, stream, text = (line.rstrip("\n").split("\t", 3) + ["", "", ""])[:4]
                ns = to_ns(ts)
                if ns is None or not (since_ns <= ns < until_ns):
                    continue
                if instance_id is not None and inst != str(instance_id):
                    continue
                if needle and needle not in text.lower():
                    continue
                if errors_only and not ERROR_RE.search(text):
                    continue
                keep.append({"ts": ts, "ns": ns, "instance_id": int(inst) if inst.isdigit() else None,
                             "stream": {"e": "stderr", "b": "build"}.get(stream, "stdout"), "text": text})
        found.extend(reversed(keep))
        if len(found) >= limit:
            break
    found.sort(key=lambda e: e["ns"], reverse=True)
    return found[:limit]


def usage(app_id: int) -> dict:
    """How much output of this app this machine keeps: bytes and the oldest day."""
    folder = _app_dir(app_id)
    days = sorted(f for f in os.listdir(folder) if f.endswith(".log")) if os.path.isdir(folder) else []
    return {"bytes": sum(os.path.getsize(os.path.join(folder, f)) for f in days),
            "oldest_day": days[0][:-4] if days else None}


# ── The loop every machine runs ───────────────────────────────────────────────

async def output_log_forever() -> None:
    """Copy the output of this machine's app containers every few seconds and prune once an hour. The primary
    and every node's agent run it for themselves, like the Docker cleanup."""
    last_prune = 0.0
    loop = asyncio.get_running_loop()
    while True:
        try:
            if await asyncio.to_thread(is_docker_available):
                await asyncio.to_thread(capture_all, _get_client())
            if loop.time() - last_prune >= PRUNE_EVERY_SECONDS:
                last_prune = loop.time()
                s = settings()
                removed = await asyncio.to_thread(prune, s["retention_days"], s["max_mb_per_app"])
                if removed:
                    log.info("[output] pruned %d old log files", removed)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("[output] %s", e)
        await asyncio.sleep(CAPTURE_EVERY_SECONDS)
