"""Startup and shutdown of the backend: first-run password, database, background tasks."""
import asyncio
import logging
import os
from transfer import full as full_restore
from contextlib import asynccontextmanager

from fastapi import FastAPI

from workers import autodeploy as workers_autodeploy
from containers import processes as pm
from core import auth, loopwatch
from core import system_config as _syscfg
from core.database import AsyncSessionLocal, init_db
from core.runtime import IS_NODE
from cron import scheduler as _cron
from nginx.panel import _load_panel_https_domain
from transfer import jobs
from notify import dispatch as notify_dispatch
from notify import watch as notify_watch
from dbs import backup_scheduler as dbs_backup_scheduler
from dbs import workers as dbs_workers
from workers.autoscaler import _autoscaler
from containers.cleanup import cleanup_forever
from nodes import local as nodes_local
from workers.demo import _dev_demo_logs
from workers.health import _node_health_monitor
from workers.mesh import _mesh_monitor
from workers.recovery import _startup_recovery
from workers.uptime import uptime_monitor
from workers.stats import _overview_sampler, _stats_collector, _stats_history_writer

log = logging.getLogger("cloudbase.main")


def _ensure_first_run_password() -> None:
    """Generate and print a password if none exists. Must run before init_db(), which
    seeds the admin user from the credentials file; otherwise the printed password
    only works after a second start."""
    if auth.load_hashed_password():
        return
    import secrets
    import string
    alphabet = string.ascii_letters + string.digits
    password = "".join(secrets.choice(alphabet) for _ in range(16))
    auth.save_hashed_password(auth.hash_password(password))
    print("\n" + "=" * 60)
    print("  Cloudbase — FIRST RUN")
    print(f"  Admin password: {password}")
    print("  Save this — it will not be shown again.")
    print("=" * 60 + "\n")


async def _start_panel_tasks() -> list[asyncio.Task]:
    """Everything a primary runs in the background."""
    await _startup_recovery()
    _load_panel_https_domain()
    tasks = [
        asyncio.create_task(nodes_local.run_forever()),
        asyncio.create_task(_stats_collector()),
        asyncio.create_task(_node_health_monitor()),
        asyncio.create_task(_mesh_monitor()),
        asyncio.create_task(_stats_history_writer()),
        asyncio.create_task(_autoscaler()),
        asyncio.create_task(cleanup_forever()),
        asyncio.create_task(_overview_sampler()),
        asyncio.create_task(workers_autodeploy.auto_deploy_loop()),
        asyncio.create_task(dbs_workers.database_monitor()),
        asyncio.create_task(dbs_backup_scheduler.backup_scheduler()),
        asyncio.create_task(dbs_workers.database_stats_sampler()),
        asyncio.create_task(dbs_workers.database_update_checker()),
        asyncio.create_task(notify_dispatch.dispatcher()),
        asyncio.create_task(notify_watch.watcher()),
        asyncio.create_task(_cron.scheduler()),
        asyncio.create_task(uptime_monitor()),
    ]
    if os.environ.get("CLOUDBASE_DEV_DEMO_LOGS"):
        tasks.append(asyncio.create_task(_dev_demo_logs(os.environ["CLOUDBASE_DEV_DEMO_LOGS"])))
    return tasks


@asynccontextmanager
async def lifespan(app: FastAPI):
    # A node runs its agent as a process of its own (node_agent.py) and serves no panel. If this web app is
    # started on one anyway, it serves nothing but the health check and starts none of the panel's loops.
    if IS_NODE:
        log.warning("INIT: this server is a node; its agent runs as 'python node_agent.py', not inside the web app")
        yield
        return

    # A full backup restored from the panel is put in place before anything reads the database (transfer/full.py)
    full_restore.apply_pending()
    _ensure_first_run_password()

    await init_db()
    jobs.sweep()      # export files left behind by a crash or restart
    pm.set_main_loop(asyncio.get_event_loop())
    loopwatch.start(asyncio.get_running_loop())     # logs where the event loop is stuck, should something ever hold it up

    # Keep DB-backed global system settings in memory (with one-time config.yaml bootstrap).
    async with AsyncSessionLocal() as _db:
        await _syscfg.bootstrap_from_config_if_needed(_db)
        await _syscfg.load_cache(_db)

    tasks = await _start_panel_tasks()

    yield
    for task in tasks:
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=4.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
