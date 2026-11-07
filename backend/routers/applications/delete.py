import asyncio
import os
import shutil
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import get_db
from core.models import ApplicationReplica
from containers import processes as pm, cleanup as containers_cleanup
from nodes.registry import ensure_local_node
from nodes import desired
from core.audit import log_audit
from core import auth as _auth
from apps.nginx_sync import _best_effort_remove_app_nginx, _best_effort_delete_app_certificate
from apps.runtime import _get_app_node, _cleanup_app_dependencies, _load_node_map, _best_effort_stop_legacy_app_container
from apps.serializers import _get_or_404

router = APIRouter(prefix="/api/apps", tags=["applications"])


@router.delete("/{app_id}")
async def delete_app(app_id: int, db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("apps.delete")), actor: str = Depends(_auth.get_current_actor)):
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)
    node = await _get_app_node(app, db, local_node)

    if not node.is_local:
        # Nothing to send: once its instances are gone from the node's list, the node removes their containers
        # (also later, when the node was offline just now)
        _best_effort_remove_app_nginx(app.name)
        _best_effort_delete_app_certificate(app)
        await _cleanup_app_dependencies(db, app.id)
        await db.delete(app)
        try:
            await db.commit()
        except IntegrityError as e:
            await db.rollback()
            raise HTTPException(409, f"App '{app.name}' cannot be deleted because it is still referenced") from e
        desired.nudge(node.id)
        if node.status == "online":
            return {"message": f"App '{app.name}' deleted"}
        return {"message": f"App '{app.name}' removed (node '{node.name}' was offline — it removes the containers when it is back)"}

    # Stop and remove all replica containers before deleting
    replica_result = await db.execute(
        select(ApplicationReplica).where(ApplicationReplica.app_id == app_id)
    )
    node_map = await _load_node_map(db)
    for replica in replica_result.scalars().all():
        r_node = node_map.get(replica.node_id)
        if r_node and r_node.is_local:
            await asyncio.to_thread(pm.stop_docker_replica, app_id, replica.id)
        elif r_node and not r_node.is_local:
            desired.nudge(r_node.id)     # the replica's row goes with the app; its node removes the container

    await _best_effort_stop_legacy_app_container(app_id)
    await asyncio.to_thread(containers_cleanup.remove_image, app_id, app.name)

    _best_effort_remove_app_nginx(app.name)
    _best_effort_delete_app_certificate(app)

    app_dir = pm.get_app_dir(app.name)
    if os.path.exists(app_dir):
        shutil.rmtree(app_dir)

    app_name = app.name
    app_id_val = app.id
    await _cleanup_app_dependencies(db, app.id)
    await db.delete(app)
    await log_audit(db, "app.delete", actor=actor, detail={"name": app_name, "app_id": app_id_val})
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(409, f"App '{app_name}' cannot be deleted because it is still referenced") from e
    return {"message": f"App '{app_name}' deleted"}
