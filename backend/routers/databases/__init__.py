"""Managed-database endpoints, one module per concern; the logic lives in dbs/."""

from fastapi import APIRouter

from routers.databases import crud
from routers.databases import lifecycle
from routers.databases import links
from routers.databases import relay

router = APIRouter()
router.include_router(crud.router)
router.include_router(lifecycle.router)
router.include_router(links.router)

relay_router = APIRouter()
relay_router.include_router(relay.relay_router)
