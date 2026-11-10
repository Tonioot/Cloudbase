"""Putting a new version live by replacing instances, and adding or removing a single instance (the autoscaler).

A new instance goes live the way every instance does: its node starts it (building the image first when it has to),
nodes/promote.py checks that the primary can reach it and that it passes the app's health check, and only then
marks it running, which puts it in nginx (apps/nginx_sync.py). So a deploy adds instances, waits until they are
live, and then removes the old ones; nothing here checks health or writes nginx itself.

Rolling and blue/green differ only in how many instances are replaced at a time: one, or all of them.
"""
import asyncio
import logging
from typing import Callable, Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps import health as app_health, nginx_sync
from apps.git import _refresh_app_source_revision
from apps.images import _image_build_kwargs, _image_port
from apps.runtime import _get_app_node, _start_instance_runtime
from apps.serializers import _get_or_404
from apps.validation import _assign_external_port
from containers import build as containers_build, naming as containers_naming, processes as pm
from core import activity
from core.audit import log_audit
from core.database import AsyncSessionLocal
from core.env_crypto import decrypt_env
from core.models import Application, Instance, Node
from nodes import desired

log = logging.getLogger("cloudbase.apps")

START_TIMEOUT_SECONDS = 900      # before the health check: the node may first download the source and build the image
PROGRESS_SECONDS = 30
POLL_SECONDS = 1.0
_OWN_LIMITS = ("docker_cpu_limit", "docker_memory_limit_mb", "docker_read_only_root", "docker_tmpfs_enabled", "docker_tmpfs_size_mb")


class RolloutFailed(RuntimeError):
    pass


async def add_instance(app: Application, node: Node, db: AsyncSession, *, like: Optional[Instance] = None,
                       source_revision: Optional[str] = None) -> Instance:
    """A new instance of the app on this node, on its node's list (committed). `like`: keep that instance's own limits."""
    instance = Instance(app_id=app.id, node_id=node.id, status="starting",
                                 external_port=await _assign_external_port(None, node.id, db))
    for key in _OWN_LIMITS if like is not None else ():
        setattr(instance, key, getattr(like, key))
    db.add(instance)
    await db.flush()
    await _start_instance_runtime(app, instance, node, decrypt_env(app.env_vars or ""), db, source_revision)
    await db.commit()
    return instance


async def wait_until_live(app_id: int, instance_id: int, push: Callable[[str], None]) -> None:
    """Wait until promote put the instance live. RolloutFailed when it fails, disappears or takes too long."""
    async with AsyncSessionLocal() as db:
        app = await db.get(Application, app_id)
    limit = START_TIMEOUT_SECONDS + app_health.settings_of(app)[2]
    loop = asyncio.get_running_loop()
    started = loop.time()
    next_progress = started + PROGRESS_SECONDS
    while True:
        async with AsyncSessionLocal() as db:
            instance = await db.get(Instance, instance_id)
        if instance is None:
            raise RolloutFailed(f"instance {instance_id} was removed")
        if instance.status == "running":
            return
        if instance.status == "error":
            raise RolloutFailed(f"instance {instance_id} failed: {instance.last_error or 'unknown error'}")
        if instance.status == "node_offline":
            raise RolloutFailed(f"the node of instance {instance_id} went offline")
        now = loop.time()
        if now - started > limit:
            raise RolloutFailed(f"instance {instance_id} was not live after {int(limit)}s")
        if now >= next_progress:
            what = (instance.substatus or "starting").replace("_", " ")
            push(f"[Deploy] Instance {instance_id}: {what} ({int(now - started)}s)…")
            next_progress += PROGRESS_SECONDS
        await asyncio.sleep(POLL_SECONDS)


async def remove_instances(app_id: int, instance_ids: list[int]) -> None:
    """Take instances off their nodes' lists (each node removes the container) and out of nginx."""
    if not instance_ids:
        return
    async with AsyncSessionLocal() as db:
        for instance_id in instance_ids:
            instance = await db.get(Instance, instance_id)
            if instance is not None:
                desired.nudge(instance.node_id)
                await db.delete(instance)
        await db.commit()
    await nginx_sync.refresh_app_nginx(app_id)


async def _build_here(app: Application, push: Callable[[str], None]) -> None:
    """Build the image on this server before anything changes, so a broken build stops the deploy right away."""
    if not app.working_dir:
        raise HTTPException(400, "No working directory — deploy the app first")
    push("[Deploy] Checking the image…")
    try:
        image, _built = await asyncio.to_thread(
            containers_build.build_image_unless_current,
            app.id, app.working_dir, lambda _aid, line: push(str(line)),
            app.app_type or "unknown", app.start_command or "", _image_port(app), **_image_build_kwargs(app),
        )
    except Exception as e:
        raise HTTPException(500, f"Failed to build image: {e}")
    app.docker_image = image


@activity.tracked("deploying")
async def replace_instances(app_id: int, db: AsyncSession, actor: str, *, batch: Optional[int]) -> dict:
    """Replace the app's running instances with new ones on the same nodes, `batch` at a time (None: all at once).

    Each new instance keeps the node and the own limits of the one it replaces. When a new instance does not get live,
    the new ones of that round are removed again and the old ones keep serving."""
    def push(line: str) -> None:
        pm._push_line(app_id, line)

    app = await _get_or_404(app_id, db)
    if not nginx_sync._has_public_nginx_domain(app):
        raise HTTPException(400, "This deploy needs a custom app domain or a configured base domain")

    running = (await db.execute(select(Instance).where(
        Instance.app_id == app_id, Instance.status == "running").order_by(Instance.id))).scalars().all()
    nodes = {n.id: n for n in (await db.execute(select(Node))).scalars().all()}
    if running:
        slots = [(old, nodes.get(old.node_id)) for old in running]
    else:
        slots = [(None, await _get_app_node(app, db))]          # nothing runs yet: one instance on the app's node
    for old, node in slots:
        if node is None:
            raise HTTPException(409, f"The node of instance {old.id} no longer exists")
        if not node.is_local and (node.status != "online" or not node.enabled):
            raise HTTPException(400, f"Node '{node.name}' is offline")
    if any(node.is_local for _, node in slots):
        await _build_here(app, push)
    source_revision = await asyncio.to_thread(_refresh_app_source_revision, app)

    size = batch or len(slots)
    replaced: list[int] = []
    for start in range(0, len(slots), size):
        group = slots[start:start + size]
        new = [await add_instance(app, node, db, like=old, source_revision=source_revision) for old, node in group]
        for instance, (old, node) in zip(new, group):
            push(f"[Deploy] Starting instance {instance.id} on '{node.name}'" + (f" to replace {old.id}…" if old else "…"))
        try:
            for instance in new:
                await wait_until_live(app_id, instance.id, push)
                push(f"[Deploy] Instance {instance.id} is live.")
        except RolloutFailed as exc:
            push(f"[Deploy] {exc} — removing the new instance(s); the previous version keeps serving.")
            await remove_instances(app_id, [r.id for r in new])
            raise HTTPException(502, f"Deploy rolled back: {exc}")
        await remove_instances(app_id, [old.id for old, _ in group if old is not None])
        for old, _ in group:
            if old is not None:
                push(f"[Deploy] Old instance {old.id} removed; its node stops it.")
        replaced += [r.id for r in new]

    app.docker_image = app.docker_image or containers_naming.image_name(app_id)
    if source_revision:
        app.image_revision = source_revision
    await log_audit(db, "app.deploy_instances", actor=actor, app_id=app_id,
                    detail={"name": app.name, "replaced": len(replaced), "batch": batch or "all"})
    await db.commit()
    push(f"[Deploy] Done: {len(replaced)} instance(s) replaced.")
    return {"status": "ok", "image": app.docker_image, "replaced": len(replaced), "instance_id": replaced[0] if replaced else None}
