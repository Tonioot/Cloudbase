"""Application endpoints, one module per concern; the services live in apps/."""

from fastapi import APIRouter

from routers.applications import crud
from routers.applications import create
from routers.applications import update
from routers.applications import delete
from routers.applications import repo
from routers.applications import certs
from routers.applications import lifecycle
from routers.applications import instances
from routers.applications import scale
from routers.applications import deploys
from routers.applications import maintenance
from routers.applications import webhook
from routers.applications import connections

router = APIRouter()
router.include_router(connections.router)
router.include_router(crud.router)
router.include_router(create.router)
router.include_router(update.router)
router.include_router(delete.router)
router.include_router(repo.router)
router.include_router(certs.router)
router.include_router(lifecycle.router)
router.include_router(instances.router)
router.include_router(scale.router)
router.include_router(deploys.router)
router.include_router(maintenance.router)
router.include_router(webhook.router)
