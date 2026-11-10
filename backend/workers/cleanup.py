import asyncio
import logging
import time as _time
from sqlalchemy import select
from core.database import AsyncSessionLocal
from core.models import Node as _Node
from containers import cleanup as containers_cleanup
from containers import client as containers_client
from nodes.liveness import run_node_docker_cleanup

log = logging.getLogger("cloudbase.main")


# ── Docker disk cleanup ───────────────────────────────────────────────────────
async def _docker_cleanup_loop():
    """Keep disks from filling up with old images and build cache.

    Every node is cleaned about once a day, and sooner when its disk gets
    full: at 80% at most hourly, at 90% with the aggressive settings.
    """
    await asyncio.sleep(600)
    last_run: dict[int, float] = {}
    while True:
        try:
            async with AsyncSessionLocal() as db:
                nodes = (await db.execute(select(_Node).where(_Node.enabled == True))).scalars().all()
            now = _time.time()
            for node in nodes:
                if node.is_local:
                    if not await asyncio.to_thread(containers_client.is_docker_available):
                        continue
                    disk = await asyncio.to_thread(containers_cleanup.disk_percent)
                else:
                    if node.status == "offline":
                        continue
                    disk = node.node_disk_percent
                since = now - last_run.get(node.id, 0)
                high = disk is not None and disk >= 80
                if since < (3600 if high else 86400):
                    continue
                last_run[node.id] = now
                res = await run_node_docker_cleanup(node, aggressive=disk is not None and disk >= 90)
                log.info("[docker-cleanup] %s: %s", node.name,
                         res.get("summary") or res.get("skipped") or res.get("errors"))
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.warning("[docker-cleanup] error: %s", e)
        await asyncio.sleep(900)
