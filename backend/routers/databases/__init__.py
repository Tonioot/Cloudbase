"""Managed-database endpoints, one module per concern; the logic lives in dbs/."""

from fastapi import APIRouter

from routers.databases import crud
from routers.databases import lifecycle
from routers.databases import links

router = APIRouter()
router.include_router(crud.router)
router.include_router(lifecycle.router)
router.include_router(links.router)
