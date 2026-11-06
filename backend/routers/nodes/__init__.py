"""Node endpoints, one module per concern; the services live in nodes/."""

from fastapi import APIRouter

from routers.nodes import agent
from routers.nodes import crud
from routers.nodes import commands
from routers.nodes import tunnel

router = APIRouter()
router.include_router(agent.router)
router.include_router(crud.router)
router.include_router(commands.router)
router.include_router(tunnel.router)
