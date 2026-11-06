import asyncio
from core.database import AsyncSessionLocal
from nodes import registry as nodes_registry, recovery as nodes_recovery

async def _node_health_monitor():
    await asyncio.sleep(5)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await nodes_registry.ensure_local_node(db)
                await nodes_recovery.mark_stale_nodes_offline(db)
                await nodes_recovery.recover_stranded_replicas(db)
        except asyncio.CancelledError:
            return
        except Exception:
            pass
        await asyncio.sleep(5)
