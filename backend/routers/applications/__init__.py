"""Application endpoints, one module per concern; the services live in apps/."""

from fastapi import APIRouter

from routers.applications import crud
from routers.applications import repo
from routers.applications import certs
from routers.applications import lifecycle
from routers.applications import replicas
from routers.applications import deploys
from routers.applications import stats_logs
from routers.applications import maintenance
from routers.applications import nginx

router = APIRouter()
router.include_router(crud.router)
router.include_router(repo.router)
router.include_router(certs.router)
router.include_router(lifecycle.router)
router.include_router(replicas.router)
router.include_router(deploys.router)
router.include_router(stats_logs.router)
router.include_router(maintenance.router)
router.include_router(nginx.router)
