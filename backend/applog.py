"""Per-app Cloudbase log: everything Cloudbase does *for* an app, kept on disk.

~/.cloudbase/logs/apps/<app_id>.log gets
  * every Cloudbase message pushed to the app's live log (pm._push_line):
    deploy steps, image builds, scaling, restarts, crash recovery, autoscaler…
    (not the app's own container output — that is the app's log, not ours);
  * server log records that concern the app, recognised by an `app_id` extra
    or by "app_id=N" / "app N" in the message (nginx, scale, tunnels, …).

Lines use the server log format, so system_logs can read and tail them.
"""
import logging
import logging.handlers
import os
import re
import threading

from system_logs import LOG_DIR, LOG_FORMAT

APP_LOG_DIR = os.path.join(LOG_DIR, "apps")

# One file handler per app. Records go straight to the handler, not through a
# logger: Python 3.13 drops records logged from inside another handler's emit,
# which is exactly where _AppRecordHandler runs.
_handlers: dict[int, logging.Handler] = {}
_lock = threading.Lock()
_FORMATTER = logging.Formatter("%(asctime)s [%(levelname)s] %(component)s: %(message)s")

# Pushed live-log lines → component, by their prefix.
_PUSH_COMPONENTS = (
    ("[Autoscaler]", "autoscaler"),
    ("[ZD]", "deploys"),
    ("[Rolling]", "deploys"),
    ("[Deploy]", "deploys"),
    ("[Docker build]", "build"),
    ("[Docker]", "build"),
    ("[Replica]", "instances"),
    ("[Nginx]", "nginx"),
)
_INSTANCE_MARKS = ("⟳", "✖", "⚠", "✓")

# "app_id=1", "app_id 1", "app=1", "for app 1" — but not "app 1x" or "apps 1".
_APP_ID_RE = re.compile(r"\bapp(?:_id)?(?:=|:? )(\d+)\b")
# Loggers whose app mentions are routine polling, not something that happened to the app.
_SKIP_LOGGERS = ("cloudbase.remote_stats", "cloudbase.stats", "cloudbase.applog")


def app_log_path(app_id: int) -> str:
    return os.path.join(APP_LOG_DIR, f"{int(app_id)}.log")


def _handler_for(app_id: int) -> logging.Handler:
    h = _handlers.get(app_id)
    if h is not None:
        return h
    with _lock:
        h = _handlers.get(app_id)
        if h is None:
            os.makedirs(APP_LOG_DIR, exist_ok=True)
            h = logging.handlers.RotatingFileHandler(
                app_log_path(app_id), maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8"
            )
            h.setFormatter(_FORMATTER)
            _handlers[app_id] = h
    return h


def write(app_id: int, message: str, level: int = logging.INFO, component: str = "cloudbase") -> None:
    try:
        record = logging.makeLogRecord({
            "name": f"cloudbase.applog.{int(app_id)}",
            "levelno": level,
            "levelname": logging.getLevelName(level),
            "msg": message,
            "component": f"cloudbase.{component}",
        })
        _handler_for(int(app_id)).handle(record)
    except Exception:
        pass  # logging must never break a deploy


def _component_for_push(line: str) -> str:
    for prefix, comp in _PUSH_COMPONENTS:
        if line.startswith(prefix):
            return comp
    if line[:1] in _INSTANCE_MARKS:
        return "instances"
    return "cloudbase"


def _level_for_push(line: str) -> int:
    low = line.lower()
    if line.startswith("✖") or "failed" in low or "error" in low:
        return logging.ERROR
    if line.startswith("⚠") or "warning" in low:
        return logging.WARNING
    return logging.INFO


def record_push(app_id: int, line: str) -> None:
    """Store a Cloudbase line that was pushed to the app's live log."""
    for part in str(line).splitlines() or [""]:
        if part.strip():
            write(app_id, part, _level_for_push(part), _component_for_push(part))


def forget(app_id: int) -> None:
    """Drop an app's log (on app delete — app ids can be reused)."""
    with _lock:
        h = _handlers.pop(int(app_id), None)
        if h is not None:
            h.close()
    base = app_log_path(app_id)
    for p in (base, base + ".1", base + ".2"):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
        except OSError:
            pass


class _AppRecordHandler(logging.Handler):
    """Copies server log records that are about one app into that app's log."""

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(_SKIP_LOGGERS) or record.levelno < logging.INFO:
            return
        app_id = getattr(record, "app_id", None)
        if getattr(record, "no_applog", False):
            return
        try:
            msg = record.getMessage()
        except Exception:
            return
        if app_id is None:
            m = _APP_ID_RE.search(msg)
            if not m:
                return
            app_id = int(m.group(1))
        if record.exc_info:
            msg += "\n" + logging.Formatter().formatException(record.exc_info)
        write(app_id, msg, record.levelno, record.name.split("cloudbase.", 1)[-1])


def install() -> None:
    """Attach the record router to the root logger (call after logging setup)."""
    root = logging.getLogger()
    if not any(isinstance(h, _AppRecordHandler) for h in root.handlers):
        root.addHandler(_AppRecordHandler())
