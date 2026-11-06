"""Database backup endpoints; the logic lives in dbs/backups.py."""

from fastapi import APIRouter

from routers.database_backups import crud
from routers.database_backups import agent
from routers.database_backups import storage

router = APIRouter()
router.include_router(crud.router)

agent_router = APIRouter()
agent_router.include_router(agent.agent_router)

storage_router = APIRouter()
storage_router.include_router(storage.storage_router)
