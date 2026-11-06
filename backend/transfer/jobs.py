"""Export jobs.

An export with database dumps can take minutes, longer than a browser request or a proxy will wait,
so it runs in the background: the page starts it, polls its progress and then downloads the file.
The finished file lives in a private folder (owner-only), is only handed to the user who started it,
is deleted after the first download and is swept away after an hour at the latest. It is not
encrypted: with secrets or data in it, it must be treated like a password file.
"""
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("cloudbase.transfer")

EXPORT_DIR = os.path.join(os.path.expanduser("~/.cloudbase"), "exports")
MAX_AGE_SECONDS = 3600


@dataclass
class ExportJob:
    id: str
    owner: str
    created: float
    state: str = "running"        # running | done | failed
    stage: str = "collect"        # collect | dump | pack | done
    message: str = "Preparing…"
    path: str = ""                # the encrypted file, once done
    size: int = 0
    apps: int = 0
    databases: int = 0

    def public(self) -> dict:
        return {"state": self.state, "stage": self.stage, "message": self.message, "size": self.size,
                "apps": self.apps, "databases": self.databases}


_jobs: dict[str, ExportJob] = {}


def _ensure_dir() -> None:
    os.makedirs(EXPORT_DIR, mode=0o700, exist_ok=True)


def sweep(now: Optional[float] = None) -> None:
    """Forget old jobs and delete leftover files (also those of a previous run of the server)."""
    now = now or time.time()
    for job_id in [j for j, job in _jobs.items() if now - job.created > MAX_AGE_SECONDS]:
        remove(_jobs[job_id])
    try:
        for name in os.listdir(EXPORT_DIR):
            path = os.path.join(EXPORT_DIR, name)
            if os.path.isfile(path) and now - os.path.getmtime(path) > MAX_AGE_SECONDS:
                os.remove(path)
    except OSError:
        pass


def create(owner: str) -> ExportJob:
    _ensure_dir()
    sweep()
    job = ExportJob(id=uuid.uuid4().hex, owner=owner, created=time.time())
    job.path = os.path.join(EXPORT_DIR, f"{job.id}.cloudbase")
    _jobs[job.id] = job
    return job


def get(job_id: str, owner: str) -> Optional[ExportJob]:
    job = _jobs.get(job_id)
    return job if job and job.owner == owner else None


def remove(job: ExportJob) -> None:
    _jobs.pop(job.id, None)
    delete_files(job)


def delete_files(job: ExportJob) -> None:
    try:
        if job.path and os.path.exists(job.path):
            os.remove(job.path)
    except OSError:
        log.warning("[transfer] could not delete %s", job.path)
