"""Cloudbase backend entry point: `uvicorn main:app`.

This file only wires things together. The endpoints live in routers/, the logic in
the service packages (apps, nodes, containers, nginx, dbs, notify, cron), the
background loops in workers/ and startup/shutdown in lifespan.py.
"""
from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware

from core import applog, system_logs
from core import config as _cfg
from core.access import _AuthMiddleware
from lifespan import lifespan
from routers import applications, audit as audit_router, cron as cron_router, domains as domains_router, files, frontend, logs, nodes, notifications as notifications_router, overview, panel_logs, panel_settings, roles as roles_router, session, transfer, users, databases as databases_router, database_backups as database_backups_router
from routers import stats as routers_stats

# Must run after the imports above: it replaces any root handlers an imported
# module installed (node_agent used to, which sent everything to node-agent.log).
system_logs.setup_logging()
applog.install()

_cfg.validate()

app = FastAPI(title="Cloudbase", version="1.0.0", lifespan=lifespan)

app.add_middleware(_AuthMiddleware)
# No CORS middleware: the panel is only used from its own origin, so browsers
# must never let another site read its API responses.
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)

# Order matters: the first route that matches a request wins (tests/test_app_structure.py
# checks that no route is shadowed).
for _router in (overview.router, session.router, users.router, panel_settings.router, panel_logs.router, transfer.router):
    app.include_router(_router)
app.include_router(cron_router.router)  # before applications: /api/apps/cron/… would match /{app_id} routes
app.include_router(applications.router)
app.include_router(nodes.router)
app.include_router(files.router)
app.include_router(logs.router)
app.include_router(routers_stats.router)
app.include_router(audit_router.router)
app.include_router(roles_router.router)
app.include_router(domains_router.router)
app.include_router(databases_router.router)
app.include_router(databases_router.relay_router)
app.include_router(database_backups_router.router)
app.include_router(database_backups_router.agent_router)
app.include_router(database_backups_router.storage_router)
app.include_router(notifications_router.router)

# Last: its catch-all route would otherwise shadow everything registered after it.
frontend.register(app)
