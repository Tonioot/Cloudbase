import asyncio
import json
import logging
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import AsyncSessionLocal
from core.models import Node, ApplicationReplica
from containers import processes as pm, naming as containers_naming, build as containers_build
from nginx import config as nginx_config, files as nginx_files, paths as nginx_paths
from nodes import desired
from core.env_crypto import decrypt_env
from core.audit import log_audit
from apps.git import _refresh_app_source_revision
from apps.images import _image_build_kwargs, _image_port
from apps.nginx_sync import _resolve_ssl_paths, _has_public_nginx_domain
from apps.runtime import _get_app_node, _start_replica_runtime
from apps.serializers import _get_or_404
from apps.validation import _assign_external_port
from apps.deploy_common import REMOTE_START_TIMEOUT_SECONDS, _wait_new_instance_healthy
from core import activity
from mesh import routing

log = logging.getLogger("cloudbase.apps")


@activity.tracked("deploying")
async def _do_zero_downtime_deploy(app_id: int, db: AsyncSession, local_node: Node, actor: str):
    app = await _get_or_404(app_id, db)

    if not _has_public_nginx_domain(app):
        raise HTTPException(400, "Zero-downtime deploy requires a custom app domain or a configured base domain")

    pm._push_line(app_id, "[ZD] Zero-downtime deploy starting…")

    # ── Determine target nodes from currently running replicas ─────────────────
    # Use replica node_id instead of app.node_id (which is null for replica-model apps).
    # Keep one entry per *running replica* so that if node A has 3 replicas, we start
    # 3 new replacements on that same node (not just 1).
    running_res = await db.execute(
        select(ApplicationReplica).where(
            ApplicationReplica.app_id == app_id,
            ApplicationReplica.status == "running",
        )
    )
    running_replicas = running_res.scalars().all()

    if running_replicas:
        # Build list of (node, old_replica) pairs — one entry per replica to replace
        unique_nids = list({r.node_id for r in running_replicas if r.node_id})
        nodes_res = await db.execute(select(Node).where(Node.id.in_(unique_nids)))
        node_map: dict[int, Node] = {n.id: n for n in nodes_res.scalars().all()}
        # target_slots: one entry per running replica we intend to replace
        target_slots: list[Node] = [
            node_map[r.node_id] for r in running_replicas if r.node_id and r.node_id in node_map
        ]
        # Deduplicate unique nodes for the build/refresh step
        target_nodes: list[Node] = list({n.id: n for n in target_slots}.values())
    else:
        # No running replicas yet — fall back to app's assigned node, create one replica
        fallback = await _get_app_node(app, db, local_node)
        target_nodes = [fallback]
        target_slots = [fallback]

    env_vars = decrypt_env(app.env_vars or "")
    source_revision = await asyncio.to_thread(_refresh_app_source_revision, app)

    # ── Step 1: Refresh / build image on every target node ───────────────────
    new_img: str | None = None
    for tnode in target_nodes:
        if tnode.is_local:
            if not app.working_dir:
                raise HTTPException(400, "No working directory — deploy the app first")
            try:
                def _push(_aid, line):
                    pm._push_line(app_id, str(line))
                new_img, _built = await asyncio.to_thread(
                    containers_build.build_image_unless_current,
                    app_id, app.working_dir, _push,
                    app.app_type or "unknown", app.start_command or "", _image_port(app),
                    **_image_build_kwargs(app),
                )
            except Exception as e:
                raise HTTPException(500, f"Failed to build image: {e}")
            app.docker_image = new_img
            if source_revision:
                app.image_revision = source_revision
        else:
            if tnode.status != "online":
                raise HTTPException(400, f"Node '{tnode.name}' is offline")
            # The node downloads the source and builds the image by itself when it starts the new instance

    # ── Step 2: Create new replica rows and start containers on each slot ────
    # new_entries: list of (node, replica_id) — kept for rollback and nginx assembly
    new_entries: list[tuple[Node, int]] = []

    for tnode in target_slots:
        new_ext_port = await _assign_external_port(None, tnode.id, db)
        new_replica = ApplicationReplica(
            app_id=app_id,
            node_id=tnode.id,
            external_port=new_ext_port,
            status="starting",
        )
        db.add(new_replica)
        await db.flush()
        await db.commit()
        new_entries.append((tnode, new_replica.id))

        pm._push_line(app_id, f"[ZD] Starting instance {new_replica.id} on '{tnode.name}'…")
        await _start_replica_runtime(app, new_replica, tnode, env_vars, db, source_revision)
        await db.commit()

    # ── Step 3: Health-check every new replica ────────────────────────────────
    for tnode, new_rid in new_entries:
        pm._push_line(app_id, f"[ZD] Waiting for instance {new_rid} on '{tnode.name}' to be built and reachable (max {REMOTE_START_TIMEOUT_SECONDS // 60} min)…")
        try:
            check_backend = await routing.wait_reachable(new_rid, REMOTE_START_TIMEOUT_SECONDS)
        except routing.Unreachable as exc:
            await _zd_rollback(app_id, local_node.id, new_entries, db)
            raise HTTPException(502, f"Instance {new_rid} on '{tnode.name}': {exc} — rolled back")
        label = f"{check_backend} (node '{tnode.name}')"

        pm._push_line(app_id, f"[ZD] Health checking instance {new_rid} on {label}…")
        healthy, reason = await _wait_new_instance_healthy(app_id, new_rid, check_backend)

        if not healthy:
            pm._push_line(app_id, f"[ZD] Instance {new_rid} failed health check ({reason}) — rolling back.")
            await _zd_rollback(app_id, local_node.id, new_entries, db)
            raise HTTPException(502, f"Instance {new_rid} on {label} failed health check: {reason} — rolled back")

        pm._push_line(app_id, f"[ZD] Instance {new_rid} healthy.")

    pm._push_line(app_id, "[ZD] All instances healthy. Switching nginx to new instances…")

    # ── Step 4: Mark new replicas running and build nginx backend list ────────
    new_backends: list[str] = []
    new_ids: set[int] = set()
    for tnode, new_rid in new_entries:
        async with AsyncSessionLocal() as _upd_db:
            r = await _upd_db.get(ApplicationReplica, new_rid)
            if r:
                r.status = "running"
                await _upd_db.commit()
                backend = routing.backend_for(r, tnode)
                if backend:
                    new_backends.append(backend)
        new_ids.add(new_rid)

    app.status = "running"
    if new_img:
        app.docker_image = new_img
    else:
        app.docker_image = containers_naming.image_name(app_id)
    if source_revision:
        app.image_revision = source_revision
    await db.flush()

    has_custom = bool(app.nginx_enabled and app.domain)
    ssl_cert = ssl_key = None
    if has_custom:
        ssl_cert, ssl_key = await asyncio.to_thread(_resolve_ssl_paths, app.ssl_cert_path, app.ssl_key_path)
    cfg = nginx_config.generate_config(
        app.name, app.domain if has_custom else None, new_backends,
        ssl_cert, ssl_key,
        app_id=app_id, mode="normal",
        extra_domains=json.loads(app.extra_domains or "[]") if has_custom else [],
        redirect_domains=json.loads(app.redirect_domains or "[]") if has_custom else [],
    )
    ok, msg = await asyncio.to_thread(nginx_files.write_nginx_config, nginx_paths.app_key(app.id), cfg)
    if not ok:
        pm._push_line(app_id, f"[ZD] Warning: nginx config update failed: {msg}")
    else:
        pm._push_line(app_id, "[ZD] Nginx switched to new instances. Stopping old instances…")

    # ── Step 5: Stop all previously-running replicas ──────────────────────────
    old_res = await db.execute(
        select(ApplicationReplica).where(
            ApplicationReplica.app_id == app_id,
            ApplicationReplica.id.not_in(new_ids),
            ApplicationReplica.status == "running",
        )
    )
    for old_r in old_res.scalars().all():
        desired.nudge(old_r.node_id or local_node.id)    # leaves the list with its row; its node removes the container
        await db.delete(old_r)
        pm._push_line(app_id, f"[ZD] Old instance {old_r.id} released; its node removes it.")

    first_new_id = new_entries[0][1] if new_entries else None
    await log_audit(db, "app.zero_downtime_deploy", actor=actor, app_id=app_id, detail={"name": app.name})
    await db.commit()

    pm._push_line(app_id, f"[ZD] Zero-downtime deploy complete. New instance(s): {[nid for _, nid in new_entries]}.")
    return {"status": "ok", "image": app.docker_image, "instance_id": first_new_id}


async def _zd_rollback(app_id: int, local_node_id: int, new_entries: list[tuple["Node", int]], db: "AsyncSession") -> None:
    """Best-effort cleanup of new replicas created during a failed ZD deploy."""
    for tnode, new_rid in new_entries:
        try:
            async with AsyncSessionLocal() as _rb_db:
                victim = await _rb_db.get(ApplicationReplica, new_rid)
                if victim:
                    desired.nudge(tnode.id)
                    await _rb_db.delete(victim)
                    await _rb_db.commit()
        except Exception as _e:
            log.warning("[ZD rollback] failed to clean up replica %d: %s", new_rid, _e)
