"""A full backup of this installation ("Everything"), and putting one back.

The file is the same as `cloudbase backup` makes (a .tar.gz with a `cloudbase/` folder), so the command line and
the panel can restore each other's backups: the Cloudbase database, the keys that decrypt environment variables and
tokens, the admin password, the Git tokens, config.yaml and the certificates. With database data it also holds a
fresh dump of every running database, as a normal backup in `cloudbase/backups/`: after the restore it is in that
database's backup list, one click from being restored.

Restoring replaces everything on this server, so it is never done while Cloudbase runs: the checked file is put
aside, Cloudbase stops, and the next start puts it in place before the database is opened (apply_pending). What was
there before is kept in ~/.cloudbase/before-restore-<time>/.
"""
import asyncio
import io
import logging
import os
import shutil
import socket
import sqlite3
import tarfile
import tempfile
import time
from datetime import datetime, timezone

from sqlalchemy import select

from core import paths as _paths
from core.audit import log_audit
from core.database import DATA_DIR, AsyncSessionLocal
from core.models import Database, DatabaseBackup
from dbs import engines as db_engines
from dbs.backups import _path, run_backup
from transfer import jobs

log = logging.getLogger("cloudbase.transfer")

DB_FILE = "cloudbase.db"
FILES = ("credentials", "secret_key", "env_key", "github_tokens.json", "config.yaml")
DIRS = ("certs", "db-tls")
ROOT = "cloudbase/"
PENDING = os.path.join(DATA_DIR, "restore-pending.tar.gz")
MAX_MEMBERS = 100_000


class RestoreError(ValueError):
    pass


def is_full_backup(path: str) -> bool:
    with open(path, "rb") as f:
        return f.read(2) == b"\x1f\x8b"          # gzip: a full backup; an export is a plain tar


# ── Making one ────────────────────────────────────────────────────────────────

async def run_full_backup(job: jobs.ExportJob, include_data: bool, actor: str) -> None:
    error, dumps = None, []
    try:
        if include_data:
            job.stage = "dump"
            async with AsyncSessionLocal() as db:
                databases = (await db.execute(select(Database).order_by(Database.name))).scalars().all()
            running = [d for d in databases if d.status in ("running", "unhealthy") and db_engines.get_engine(d.engine).backup]
            for index, d in enumerate(running):
                job.message = f"Dumping database {d.name} ({index + 1} of {len(running)})…"
                backup_id = await run_backup(d.id, "export", actor)
                async with AsyncSessionLocal() as db:
                    dumps.append(_path(await db.get(DatabaseBackup, backup_id)))
            job.databases = len(dumps)
        job.stage, job.message = "pack", "Packing…"
        await asyncio.to_thread(pack, job.path, dumps)
        os.chmod(job.path, 0o600)
        job.size = os.path.getsize(job.path)
    except Exception as exc:
        error = str(getattr(exc, "detail", exc))[:300]
        log.warning("[transfer] full backup failed: %s", error)
        jobs.delete_files(job)
    try:
        async with AsyncSessionLocal() as db:
            await log_audit(db, "transfer.full_backup", actor=actor,
                            detail={"failed": True, "error": error} if error else {"data": len(dumps)})
            await db.commit()
    finally:
        if error:
            job.path, job.state, job.message = "", "failed", error
        else:
            job.state, job.stage, job.message = "done", "done", "Ready to download"


def pack(out: str, dumps: list[str]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        copy = os.path.join(tmp, DB_FILE)
        src, dst = sqlite3.connect(os.path.join(DATA_DIR, DB_FILE)), sqlite3.connect(copy)
        try:
            src.backup(dst)                      # a consistent copy, also while Cloudbase writes
        finally:
            dst.close()
            src.close()
        version = open(_paths.VERSION_FILE).read().strip() if os.path.exists(_paths.VERSION_FILE) else "dev"
        manifest = (f"version={version}\ncreated={datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
                    f"host={socket.gethostname()}\nkind=full\ndata={len(dumps)}\n").encode()
        with tarfile.open(out, "w:gz") as tar:
            info = tarfile.TarInfo(ROOT + "MANIFEST")        # first: the page reads it before restoring
            info.size = len(manifest)
            tar.addfile(info, io.BytesIO(manifest))
            tar.add(copy, ROOT + DB_FILE)
            for name in FILES:
                if os.path.isfile(os.path.join(DATA_DIR, name)):
                    tar.add(os.path.join(DATA_DIR, name), ROOT + name)
            for name in DIRS:
                if os.path.isdir(os.path.join(DATA_DIR, name)):
                    tar.add(os.path.join(DATA_DIR, name), ROOT + name)
            for dump in dumps:
                tar.add(dump, ROOT + "backups/" + os.path.basename(dump))


# ── Checking and staging one ──────────────────────────────────────────────────

def _target(name: str) -> str | None:
    """Where a member goes in ~/.cloudbase, or None when it is not something a backup may put back."""
    if not name.startswith(ROOT) or name.startswith("/") or "\\" in name:
        return None
    rel = name[len(ROOT):].strip("/")
    parts = rel.split("/")
    if not rel or any(p in ("", ".", "..") for p in parts):
        return None
    if rel in (DB_FILE, *FILES) or parts[0] in (*DIRS, "backups"):
        return rel
    return None                                   # MANIFEST, a node's agent_state.json, anything else: left out


def check(path: str) -> dict:
    """What the backup says about itself. Raises RestoreError when it is not a usable full backup."""
    try:
        tar = tarfile.open(path, "r:gz")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise RestoreError(f"This is not a Cloudbase backup ({exc})")
    with tar:
        members = tar.getmembers()
        if len(members) > MAX_MEMBERS:
            raise RestoreError("This backup has too many files")
        names = {m.name.strip("/") for m in members}
        if ROOT + DB_FILE not in names:
            raise RestoreError("This is not a Cloudbase backup: cloudbase/cloudbase.db is missing")
        for m in members:
            if (m.issym() or m.islnk() or m.isdev()) and _target(m.name):
                raise RestoreError(f"This backup contains a link ({m.name}); it is not restored")
        info = {}
        manifest = tar.extractfile(ROOT + "MANIFEST") if ROOT + "MANIFEST" in names else None
        if manifest:
            for line in manifest.read(4096).decode(errors="replace").splitlines():
                key, _, value = line.partition("=")
                info[key.strip()] = value.strip()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, DB_FILE)
            with open(db_path, "wb") as out:
                shutil.copyfileobj(tar.extractfile(ROOT + DB_FILE), out)
            try:
                con = sqlite3.connect(db_path)
                try:
                    if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RestoreError("The Cloudbase database in this backup is damaged")
                    info["users"] = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
                    info["apps"] = con.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
                finally:
                    con.close()
            except sqlite3.DatabaseError as exc:
                raise RestoreError(f"The Cloudbase database in this backup cannot be read ({exc})")
        info["env_key"] = ROOT + "env_key" in names
        info["data"] = sum(1 for n in names if n.startswith(ROOT + "backups/"))
    return info


def stage(path: str) -> None:
    """Put a checked backup where the next start picks it up."""
    os.replace(path, PENDING)


# ── Putting it in place (at start) ────────────────────────────────────────────

def apply_pending() -> bool:
    """At start, before the database is opened: put a staged backup in place. True when one was restored."""
    if not os.path.exists(PENDING):
        return False
    before = os.path.join(DATA_DIR, f"before-restore-{time.strftime('%Y%m%d-%H%M%S')}")
    os.makedirs(before, mode=0o700)
    # What is there now is kept aside; the database's journal files must go too, or they would be applied to the new one
    for name in (DB_FILE, DB_FILE + "-wal", DB_FILE + "-shm", *FILES, *DIRS):
        src = os.path.join(DATA_DIR, name)
        if os.path.exists(src):
            shutil.move(src, os.path.join(before, name))
    with tarfile.open(PENDING, "r:gz") as tar:
        for m in tar.getmembers():
            rel = _target(m.name)
            if rel is None or not (m.isfile() or m.isdir()):
                continue
            dest = os.path.join(DATA_DIR, *rel.split("/"))
            if m.isdir():
                os.makedirs(dest, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with tar.extractfile(m) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            os.chmod(dest, 0o600)
    os.remove(PENDING)
    log.warning("[restore] a full backup was restored; what was here before is in %s", before)
    return True


def leave_for_restart(delay: float = 1.0) -> None:
    """Stop, so the service manager starts Cloudbase again and the staged backup is put in place. A non-zero exit
    code: systemd restarts on failure only."""
    asyncio.get_running_loop().call_later(delay, os._exit, 3)

