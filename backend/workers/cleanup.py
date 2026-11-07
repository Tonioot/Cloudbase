import asyncio
import logging
import time as _time
from sqlalchemy import select
from core.database import AsyncSessionLocal
from core.models import Node as _Node, ApplicationReplica, NodeCommand as _NC
from containers import replicas as containers_replicas
from containers import cleanup as containers_cleanup
from containers import client as containers_client
from nodes.liveness import run_node_docker_cleanup

log = logging.getLogger("cloudbase.main")


# ── NodeCommand cleanup ───────────────────────────────────────────────────────
async def _node_command_cleanup():
    """Delete completed/failed NodeCommands older than 24h every hour.
    Keeps the table small so scans for 'queued' commands stay fast.
    """
    import datetime as _dt
    from sqlalchemy import delete as _del
    await asyncio.sleep(60)
    while True:
        try:
            cutoff = _dt.datetime.utcnow() - _dt.timedelta(hours=24)
            async with AsyncSessionLocal() as db:
                await db.execute(
                    _del(_NC).where(
                        _NC.status.in_(["done", "failed"]),
                        _NC.completed_at < cutoff,
                    )
                )
                await db.commit()
        except asyncio.CancelledError:
            return
        except Exception:
            pass
        await asyncio.sleep(3600)


# ── Orphan replica cleanup ────────────────────────────────────────────────────
async def _orphan_replica_cleanup():
    """Every 30 min reconcile local replica containers against the DB and remove
    leaks: dead/exited replica containers, and running replica containers that no
    longer have a corresponding ApplicationReplica row. Prevents replicas from
    piling up on the host (which starves memory and destabilises container DNS).
    """

    await asyncio.sleep(120)
    while True:
        try:
            containers = await asyncio.to_thread(containers_replicas.list_replica_containers)
            if containers:
                async with AsyncSessionLocal() as db:
                    # Local node id — we only manage local containers here.
                    nr = await db.execute(select(_Node).where(_Node.is_local == True))
                    local_node = nr.scalar_one_or_none()
                    local_node_id = local_node.id if local_node else None

                    # Live replica ids that are expected to be running locally.
                    rep_result = await db.execute(
                        select(ApplicationReplica).where(
                            ApplicationReplica.status.in_(["pending", "starting", "running", "stopping"])
                        )
                    )
                    live = rep_result.scalars().all()
                    live_ids = {
                        r.id for r in live
                        if local_node_id is None or r.node_id is None or r.node_id == local_node_id
                    }

                    for c in containers:
                        rid = c["replica_id"]
                        is_dead = c["status"] in ("exited", "dead", "created")
                        is_orphan = rid not in live_ids
                        if is_dead or is_orphan:
                            removed = await asyncio.to_thread(containers_replicas.remove_container_by_name, c["name"])
                            if removed:
                                reason = "dead" if is_dead else "no DB row"
                                log.info("[orphan-cleanup] removed replica container %s (%s)", c["name"], reason)
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.warning("[orphan-cleanup] error: %s", e)
        await asyncio.sleep(1800)


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
