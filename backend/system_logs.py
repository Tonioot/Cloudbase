"""Cloudbase system logs: logging setup plus reading/tailing every log source.

All Cloudbase components log through the stdlib `logging` module under
`cloudbase.<component>` loggers. setup_logging() sends them to stdout (journal)
and a rotating ~/.cloudbase/logs/server.log, in one format that carries the
component name, so the System logs page can filter per component.
"""
import asyncio
import logging
import logging.handlers
import os
import re
from typing import AsyncIterator, Optional

LOG_DIR = os.path.expanduser("~/.cloudbase/logs")
SERVER_LOG = os.path.join(LOG_DIR, "server.log")
AGENT_LOG = os.path.join(LOG_DIR, "node-agent.log")
CLI_LOG = os.path.join(LOG_DIR, "cloudbase-cli.log")

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# key → (label, path, description)
SOURCES: dict[str, tuple[str, str, str]] = {
    "server": ("Cloudbase server", SERVER_LOG,
               "Everything the Cloudbase server does: deploys, autoscaler, nginx, nodes, databases, backups, …"),
    "agent": ("Node agent", AGENT_LOG,
              "The node agent loop (only active when this server runs as a node)."),
    "cli": ("Start / update script", CLI_LOG,
            "Output of start.sh: install, start, stop and update runs."),
    "nginx_error": ("Nginx errors", "/var/log/nginx/error.log",
                    "Nginx error log: upstream failures (502s), config and TLS problems."),
    "nginx_access": ("Nginx access", "/var/log/nginx/access.log",
                     "Every HTTP request nginx served, for the panel and all apps."),
}

# Loggers that log every request/poll at INFO and would drown everything else.
_NOISY_LOGGERS = ("httpx", "httpcore", "urllib3", "docker", "asyncio", "multipart")

# Logger names are dotted ("cloudbase.autoscaler", "uvicorn.error") or a known
# bare name; this keeps "queue_command: …" in old-format lines a message.
_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d+)?) \[(?P<level>[A-Z]+)\] "
    r"(?:(?P<name>[\w\-]+(?:\.[\w\-]+)+|root|httpx|httpcore|urllib3|docker|asyncio|uvicorn): )?(?P<msg>.*)$"
)
_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


def setup_logging() -> None:
    """Configure root logging. force=True replaces whatever an imported module set up."""
    os.makedirs(LOG_DIR, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        SERVER_LOG, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    logging.basicConfig(
        level=logging.INFO,
        format=LOG_FORMAT,
        handlers=[logging.StreamHandler(), file_handler],
        force=True,
    )
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def component_of(logger_name: Optional[str]) -> str:
    """'cloudbase.autoscaler' → 'autoscaler'; lines without a logger name → 'other'."""
    if not logger_name:
        return "other"
    for prefix in ("cloudbase.", "pdm."):
        if logger_name.startswith(prefix):
            return logger_name[len(prefix):]
    return logger_name


def _parse_entries(lines: list[str]) -> list[dict]:
    """Group raw lines into entries; lines without a timestamp (tracebacks) join the previous one."""
    entries: list[dict] = []
    for raw in lines:
        line = raw.rstrip("\n")
        m = _LINE_RE.match(line)
        if m:
            entries.append({
                "ts": m.group("ts"),
                "level": m.group("level"),
                "component": component_of(m.group("name")),
                "text": line,
            })
        elif entries:
            entries[-1]["text"] += "\n" + line
        elif line:
            entries.append({"ts": None, "level": None, "component": "other", "text": line})
    return entries


def _matches(e: dict, component: Optional[str], min_level: int, q: Optional[str]) -> bool:
    if component and e["component"] != component:
        return False
    if min_level and _LEVELS.get(e["level"] or "", 0) < min_level:
        return False
    if q and q not in e["text"].lower():
        return False
    return True


def _read_backwards(path: str, max_bytes: int) -> list[str]:
    """Last max_bytes of a file as lines (first, possibly partial, line dropped)."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        start = max(0, size - max_bytes)
        f.seek(start)
        data = f.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    return lines


def source_info() -> list[dict]:
    out = []
    for key, (label, path, desc) in SOURCES.items():
        exists = os.path.exists(path)
        out.append({
            "key": key,
            "label": label,
            "description": desc,
            "path": path,
            "exists": exists,
            "readable": exists and os.access(path, os.R_OK),
            "size": os.path.getsize(path) if exists else 0,
        })
    return out


def resolve_path(source: str) -> str:
    """Path of a log source: a SOURCES key, or "app:<id>" for an app's Cloudbase log."""
    if source in SOURCES:
        return SOURCES[source][1]
    if source.startswith("app:") and source[4:].isdigit():
        return os.path.join(LOG_DIR, "apps", f"{int(source[4:])}.log")
    raise ValueError(f"Unknown log source '{source}'")


def read_entries(
    source: str,
    limit: int = 500,
    component: Optional[str] = None,
    level: Optional[str] = None,
    q: Optional[str] = None,
) -> dict:
    """Newest `limit` entries matching the filters, oldest first.

    Scans back through the file in growing chunks (and into the first rotated
    file) until enough entries match, so a filter on a quiet component still
    finds its entries in a busy log.
    """
    path = resolve_path(source)
    min_level = _LEVELS.get((level or "").upper(), 0)
    q = (q or "").strip().lower() or None
    limit = max(1, min(limit, 5000))

    if not os.path.exists(path):
        return {"entries": [], "components": [], "truncated": False}

    budget = 32 * 1024 * 1024  # never scan more than this per request
    size = os.path.getsize(path)
    chunk = 512 * 1024
    while True:
        take = min(size, chunk, budget)
        entries = _parse_entries(_read_backwards(path, take))
        matched = [e for e in entries if _matches(e, component, min_level, q)]
        if len(matched) >= limit or take >= size or take >= budget:
            break
        chunk *= 4
    components = {e["component"] for e in entries}

    # Whole current file read and still short: continue in the rotated file.
    rotated = path + ".1"
    if len(matched) < limit and take >= size and budget > take and os.path.exists(rotated):
        older = _parse_entries(_read_backwards(rotated, min(os.path.getsize(rotated), budget - take)))
        components.update(e["component"] for e in older)
        matched = [e for e in older if _matches(e, component, min_level, q)] + matched

    return {
        "entries": matched[-limit:],
        "components": sorted(components),
        "truncated": len(matched) > limit,
    }


async def tail(source: str, poll_seconds: float = 0.5) -> AsyncIterator[list[dict]]:
    """Yield batches of new entries appended to a source; follows log rotation.

    Continuation lines (tracebacks) that arrive in a later batch than their
    entry come out with "cont": True and the component/level of that entry.
    """
    path = resolve_path(source)
    f = None
    inode = None
    pending = ""
    last: Optional[dict] = None
    try:
        while True:
            if f is None:
                try:
                    f = open(path, "r", encoding="utf-8", errors="replace")
                    st = os.fstat(f.fileno())
                    inode = st.st_ino
                    f.seek(0, os.SEEK_END)
                except FileNotFoundError:
                    f = None
                    await asyncio.sleep(2)
                    continue
            chunk = f.read()
            if chunk:
                pending += chunk
                *complete, pending = pending.split("\n")
                if complete:
                    batch = _parse_entries(complete)
                    if batch and batch[0]["ts"] is None and last is not None:
                        batch[0].update(component=last["component"], level=last["level"], cont=True)
                    if batch:
                        last = batch[-1]
                    yield batch
                continue
            await asyncio.sleep(poll_seconds)
            # Rotated (new inode) or truncated → reopen from the start of the new file.
            try:
                st = os.stat(path)
                if st.st_ino != inode or st.st_size < f.tell():
                    f.close()
                    f = open(path, "r", encoding="utf-8", errors="replace")
                    inode = os.fstat(f.fileno()).st_ino
                    pending = ""
            except FileNotFoundError:
                pass
    finally:
        if f is not None:
            f.close()
