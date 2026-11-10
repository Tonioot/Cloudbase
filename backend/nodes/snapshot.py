"""The list of instances a node asks for (see desired.py for what the list means)."""
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.env_crypto import decrypt_text
from core.models import Instance
from nodes.desired import list_hash


async def snapshot(db: AsyncSession, node_id: int) -> dict:
    """The list a node asks for: every instance placed on it, with what it should do."""
    instances = (await db.execute(
        select(Instance).where(Instance.node_id == node_id).order_by(Instance.id)
    )).scalars().all()
    entries = []
    for instance in instances:
        wanted = instance.desired or "stopped"
        spec = json.loads(decrypt_text(instance.spec_json)) if wanted == "running" and instance.spec_json else None
        if wanted == "running" and spec is None:
            wanted = "stopped"                     # its app is gone; there is nothing to run
        entries.append({"instance_id": instance.id, "app_id": instance.app_id, "desired": wanted,
                        "spec_hash": instance.spec_hash if spec else None, "spec": spec})
    return {"hash": list_hash(entries), "instances": entries}
