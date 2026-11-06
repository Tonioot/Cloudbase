"""Builds an export file in the background (see jobs.py)."""
import asyncio
import logging
import os
from typing import Optional

from core.audit import log_audit
from core.database import AsyncSessionLocal
from transfer import archive, crypto, databases, jobs, manifest

log = logging.getLogger("cloudbase.transfer")


async def run_export(
    job: jobs.ExportJob,
    *,
    app_ids: Optional[list[int]],
    database_ids: Optional[list[int]],
    include_secrets: bool,
    include_data: bool,
    passphrase: str,
    actor: str,
) -> None:
    tar_path = job.path + ".tar"
    error: Optional[str] = None
    detail: dict = {}
    try:
        job.message = "Collecting settings…"
        async with AsyncSessionLocal() as db:
            content = await manifest.build_manifest(db, app_ids, include_secrets)
            content["databases"], ids = await databases.database_entries(db, database_ids)
        files = {}
        if include_data and content["databases"]:
            files = await databases.dump_into(content["databases"], ids, actor, lambda text: setattr(job, "message", text))
        job.apps, job.databases = len(content["apps"]), len(content["databases"])

        job.message = "Packing…"
        await asyncio.to_thread(archive.write, tar_path, content, files)
        os.chmod(tar_path, 0o600)             # it holds everything in the clear until it is encrypted
        job.message = "Encrypting…"
        await asyncio.to_thread(crypto.encrypt_file, tar_path, job.path, passphrase)
        os.chmod(job.path, 0o600)
        job.size = os.path.getsize(job.path)
        detail = {"apps": job.apps, "databases": job.databases, "secrets": include_secrets, "data": bool(files)}
    except Exception as exc:
        error = str(getattr(exc, "detail", exc))[:300]
        log.warning("[transfer] export failed: %s", error)
        jobs.delete_files(job)                # the job itself stays, so the page can show why it failed
        detail = {"failed": True, "error": error}
    finally:
        if os.path.exists(tar_path):
            os.remove(tar_path)               # the unencrypted archive must never stay behind

    try:
        async with AsyncSessionLocal() as db:
            await log_audit(db, "transfer.export", actor=actor, detail=detail)   # never the content
            await db.commit()
    finally:
        # last, so a page that sees "done" can rely on everything else being written
        if error:
            job.path, job.state, job.message = "", "failed", error
        else:
            job.state, job.message = "done", "Ready to download"
