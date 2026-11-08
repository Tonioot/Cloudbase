"""ONE-OFF MIGRATION, to be deleted once every server has run it (see containers/legacy_names.py).

On the primary: every app gets its source folder and image under the new, id-based names, its stored paths are
updated and the nginx config that carried the app's name is removed (the new one is written at startup).
"""
import asyncio
import logging
import os

from sqlalchemy import select

from containers import legacy_names, naming as containers_naming, processes as pm
from core.database import AsyncSessionLocal
from core.models import Application
from nginx import files as nginx_files, paths as nginx_paths

log = logging.getLogger("cloudbase.migrate")


async def run() -> None:
    async with AsyncSessionLocal() as db:
        for app in (await db.execute(select(Application))).scalars().all():
            try:
                done = await asyncio.to_thread(legacy_names.adopt, app.id, app.name)
                old_dir = legacy_names.old_app_dir(app.name)
                if app.working_dir and app.working_dir.rstrip("/\\") == old_dir:
                    app.working_dir = pm.get_app_dir(app.id)
                if app.docker_image == legacy_names.old_image_name(app.id, app.name):
                    app.docker_image = containers_naming.image_name(app.id)
                old_nginx = nginx_paths._safe_name(app.name)
                still_there = any(os.path.lexists(os.path.join(d, old_nginx)) for d in (nginx_paths.NGINX_SITES_DIR, nginx_paths.NGINX_ENABLED_DIR))
                if old_nginx != nginx_paths.app_key(app.id) and still_there:
                    await asyncio.to_thread(nginx_files.remove_nginx_config, old_nginx)
                    done.append(f"nginx config {old_nginx}")
                if done:
                    log.info("[migrate] app %d (%s): %s", app.id, app.name, ", ".join(done))
            except Exception as exc:
                log.warning("[migrate] app %d (%s) not fully moved: %s", app.id, app.name, exc)
        await db.commit()
