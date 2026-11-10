"""What runs on a node: its instances and databases, for the node's page."""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Application, Instance, Database, Node
from mesh import routing


async def workloads(db: AsyncSession, node: Node) -> dict:
    """The instances placed on the node (with where the primary reaches each) and the databases that live on it."""
    rows = (await db.execute(
        select(Instance, Application)
        .join(Application, Application.id == Instance.app_id)
        .where(Instance.node_id == node.id)
        .order_by(Application.name, Instance.id)
    )).all()
    instances = [{
        "id": instance.id,
        "app_id": app.id,
        "app_name": app.name,
        "status": instance.status,
        "substatus": instance.substatus,
        "last_error": instance.last_error,
        "port": instance.external_port,
        "address": routing.backend_for(instance, node),
    } for instance, app in rows]
    databases = [{
        "id": d.id, "name": d.name, "engine": d.engine, "version": d.version, "status": d.status,
    } for d in (await db.execute(select(Database).where(Database.node_id == node.id).order_by(Database.name))).scalars().all()]
    return {"instances": instances, "databases": databases}
