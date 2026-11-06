"""Builds an export file in the background (see jobs.py)."""
import asyncio
import logging
import os
from typing import Optional

from core.audit import log_audit
from core.database import AsyncSessionLocal
from transfer import archive, databases, jobs, manifest

log = logging.getLogger("cloudbase.transfer")


async def run_export(
    job: jobs.ExportJob,
    *,
    app_ids: Optional[list[int]],
    database_ids: Optional[list[int]],
    include_secrets: bool,
    include_data: bool,
    actor: str,
) -> None:
    error: Optional[str] = None
    detail: dict = {}
    try:
        job.stage, job.message = "collect", "Collecting settings…"
        async with AsyncSessionLocal() as db:
            content = await manifest.build_manifest(db, app_ids, include_secrets)
            content["databases"], ids = await databases.database_entries(db, database_ids)
        files = {}
        if include_data and content["databases"]:
            job.stage = "dump"
            files = await databases.dump_into(content["databases"], ids, actor, lambda text: setattr(job, "message", text))
        job.apps, job.databases = len(content["apps"]), len(content["databases"])

        job.stage, job.message = "pack", "Packing…"
        await asyncio.to_thread(archive.write, job.path, content, files)
        os.chmod(job.path, 0o600)             # it may hold secrets and data in the clear: owner only
        job.size = os.path.getsize(job.path)
        detail = {"apps": job.apps, "databases": job.databases, "secrets": include_secrets, "data": bool(files)}
    except Exception as exc:
        error = str(getattr(exc, "detail", exc))[:300]
        log.warning("[transfer] export failed: %s", error)
        jobs.delete_files(job)                # the job itself stays, so the page can show why it failed
        detail = {"failed": True, "error": error}

    try:
        async with AsyncSessionLocal() as db:
            await log_audit(db, "transfer.export", actor=actor, detail=detail)   # never the content
            await db.commit()
    finally:
        # last, so a page that sees "done" can rely on everything else being written
        if error:
            job.path, job.state, job.message = "", "failed", error
        else:
            job.state, job.stage, job.message = "done", "done", "Ready to download"
