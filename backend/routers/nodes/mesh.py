"""What the panel shows about the node network."""
from fastapi import APIRouter, Depends

from core import auth as _auth
from mesh import hub

router = APIRouter(prefix="/api/nodes", tags=["nodes"])


@router.get("/mesh")
async def mesh_info(_user: dict = Depends(_auth.require_permission("nodes.view"))):
    return hub.info()
