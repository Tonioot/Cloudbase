"""Putting a new version live by replacing instances, and adding or removing a single instance (the autoscaler).

A new instance goes live the way every instance does: its node starts it (building the image first when it has to),
nodes/promote.py checks that the primary can reach it and that it passes the app's health check, and only then
marks it running, which puts it in nginx (apps/nginx_sync.py). So a deploy adds instances, waits until they are
live, and then removes the old ones; nothing here checks health or writes nginx itself.

Rolling and blue/green differ only in how many instances are replaced at a time: one, or all of them.
"""
import asyncio
import contextlib
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
from core.models import Application, Instance, Node
from nodes import capacity, desired
from core import projects as core_projects

log = logging.getLogger("cloudbase.apps")

START_TIMEOUT_SECONDS = 900      # before the health check: the node may first download the source and build the image
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
    await _start_instance_runtime(app, instance, node, core_projects.app_env(app), db, source_revision)
    await db.commit()
    return instance


async def wait_until_live(app_id: int, instance_id: int) -> None:
    """Wait until promote put the instance live. RolloutFailed when it fails, disappears or takes too long.
    (How far it is shows in the app's log: nodes/observed.py and nodes/promote.py write a line for every step.)"""
    async with AsyncSessionLocal() as db:
        app = await db.get(Application, app_id)
    limit = START_TIMEOUT_SECONDS + app_health.settings_of(app)[2]
    loop = asyncio.get_running_loop()
    started = loop.time()
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
        if loop.time() - started > limit:
            raise RolloutFailed(f"instance {instance_id} was not live after {int(limit)}s")
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
    reserving = capacity.enabled()
    for old, node in slots:
        if node is None:
            raise HTTPException(409, f"The node of instance {old.id} no longer exists")
        if not reserving and not node.is_local and (node.status != "online" or not node.enabled):
            raise HTTPException(400, f"Node '{node.name}' is offline")     # with reservations another node takes over
    built_here = any(node.is_local for _, node in slots)
    if built_here:
        await _build_here(app, push)
    source_revision = await asyncio.to_thread(_refresh_app_source_revision, app)

    size = batch or len(slots)
    replaced: list[int] = []
    pending = list(slots)
    while pending:
        group, new, new_ids = [], [], []
        try:
            for old, node in pending[:size]:
                target = node
                if reserving:
                    need = capacity.running_need(old, app) if old is not None else capacity.app_need(app)
                    target = await capacity.deploy_target(db, node, need, surge=old is not None)
                    if target is None:
                        if group:
                            break       # first the ones that have room: the old ones they replace make room for the rest
                        target = await capacity.wait_for_deploy_target(node, need, surge=old is not None,
                                                                       say=lambda line: push(f"[Deploy] {line}"))
                    if target.id != node.id:
                        push(f"[Deploy] No room on '{node.name}': the new instance goes to '{target.name}'.")
                    if target.is_local and not built_here:
                        await _build_here(app, push)
                        built_here = True
                # next to an old one: the new version may use the room kept for deploys (nodes/capacity.py)
                with capacity.surge() if old is not None and target.id == node.id else contextlib.nullcontext():
                    instance = await add_instance(app, target, db, like=old, source_revision=source_revision)
                group.append((old, node))
                new.append((instance, target))
                new_ids.append(instance.id)
        except HTTPException:
            await db.rollback()
            await remove_instances(app_id, new_ids)
            raise
        for (instance, target), (old, _) in zip(new, group):
            push(f"[Deploy] Starting instance {instance.id} on '{target.name}'" + (f" to replace {old.id}…" if old else "…"))
        try:
            for instance_id in new_ids:
                await wait_until_live(app_id, instance_id)
        except RolloutFailed as exc:
            push(f"[Deploy] {exc} — removing the new instance(s); the previous version keeps serving.")
            await remove_instances(app_id, new_ids)
            raise HTTPException(502, f"Deploy rolled back: {exc}")
        await remove_instances(app_id, [old.id for old, _ in group if old is not None])
        for old, _ in group:
            if old is not None:
                push(f"[Deploy] Old instance {old.id} removed; its node stops it.")
        replaced += new_ids
        pending = pending[len(group):]

    app.docker_image = app.docker_image or containers_naming.image_name(app_id)
    if source_revision:
        app.image_revision = source_revision
    await log_audit(db, "app.deploy_instances", actor=actor, app_id=app_id,
                    detail={"name": app.name, "replaced": len(replaced), "batch": batch or "all"})
    await db.commit()
    push(f"[Deploy] Done: {len(replaced)} instance(s) replaced.")
    return {"status": "ok", "image": app.docker_image, "replaced": len(replaced), "instance_id": replaced[0] if replaced else None}
