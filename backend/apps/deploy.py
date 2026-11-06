import asyncio
import contextlib
import json
import logging
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import AsyncSessionLocal
from core.models import ApplicationReplica, Node
from containers import processes as pm, manager as dm
from nginx import manager as nm
from nodes.registry import ensure_local_node
from nodes.commands import queue_node_command, wait_for_node_command
from core.env_crypto import decrypt_env
from core.audit import log_audit
from apps.git import _refresh_app_source_revision
from apps.images import _image_build_kwargs, _image_port
from apps.nginx_sync import _has_public_nginx_domain, _local_http_service_ready, _resolve_ssl_paths
from apps.runtime import _docker_runtime_options, _get_app_node, _remote_replica_command_payload, _start_instance_local
from apps.serializers import _get_or_404
from apps.state import DEPLOY_HEALTH_PROGRESS_SECONDS, DEPLOY_HEALTH_TIMEOUT_SECONDS, _deploys_in_progress, _scaling_in_progress
from apps.validation import _assign_external_port

log = logging.getLogger("cloudbase.apps")


@contextlib.contextmanager
def _single_deploy(app_id: int):
    """Reject a second rolling / blue-green deploy while one is running for this app.

    These requests take minutes; without this a retry (or a second click after a
    proxy timeout) starts a parallel deploy that races the first one.
    """
    if app_id in _deploys_in_progress:
        raise HTTPException(409, "A deploy is already in progress for this app — wait for it to finish.")
    if app_id in _scaling_in_progress:
        raise HTTPException(409, "The autoscaler is adding or removing an instance for this app — try again in a moment.")
    _deploys_in_progress.add(app_id)
    try:
        yield
    finally:
        _deploys_in_progress.discard(app_id)


async def _do_rolling_deploy(app_id: int, db: AsyncSession, actor: str):
    """Rolling deploy: replace each running replica one at a time with a freshly built image.
    While one replica is cycling, the others keep serving traffic — no downtime page is shown.
    Falls back to a normal ZD deploy when only one replica is running.
    """
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)

    if not _has_public_nginx_domain(app):
        raise HTTPException(400, "Rolling deploy requires a custom app domain or a configured base domain")

    running_res = await db.execute(
        select(ApplicationReplica).where(
            ApplicationReplica.app_id == app_id,
            ApplicationReplica.status == "running",
        )
    )
    running_replicas = running_res.scalars().all()

    if len(running_replicas) < 2:
        pm._push_line(app_id, "[Rolling] Only one replica — delegating to Blue/Green deploy…")
        return await _do_zero_downtime_deploy(app_id, db, local_node, actor)

    pm._push_line(app_id, f"[Rolling] Starting rolling deploy across {len(running_replicas)} replica(s)…")

    env_vars = decrypt_env(app.env_vars or "")
    source_revision = _refresh_app_source_revision(app)

    unique_nids = list({r.node_id for r in running_replicas if r.node_id})
    nodes_res = await db.execute(select(Node).where(Node.id.in_(unique_nids)))
    node_map: dict[int, Node] = {n.id: n for n in nodes_res.scalars().all()}

    # Build once per unique node
    built_nodes: set[int | None] = set()
    new_img: str | None = None
    for replica in running_replicas:
        tnode = node_map.get(replica.node_id) if replica.node_id else local_node
        if tnode is None or tnode.id in built_nodes:
            continue
        built_nodes.add(tnode.id)
        if tnode.is_local:
            if not app.working_dir:
                raise HTTPException(400, "No working directory — deploy the app first")
            pm._push_line(app_id, "[Rolling] Building new image…")
            try:
                def _push(_aid, line):
                    pm._push_line(app_id, str(line))
                new_img = await asyncio.to_thread(
                    dm.build_image,
                    app_id, app.name, app.working_dir, _push,
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
            pm._push_line(app_id, f"[Rolling] Refreshing source on node '{tnode.name}'…")
            refresh_cmd = await queue_node_command(
                db, node_id=tnode.id, app_id=app_id,
                command_type="refresh_source",
                payload={
                    "app_name": app.name,
                    "app_id": app_id,
                    "app_type": app.app_type or "unknown",
                    "start_command": app.start_command or "",
                    "build_command": app.build_command or "",
                    "internal_port": _image_port(app),
                    "env_vars": env_vars,
                    "docker_options": _docker_runtime_options(app),
                    "source_revision": source_revision,
                },
            )
            refresh_done = await wait_for_node_command(db, refresh_cmd.id, timeout_seconds=300)
            if refresh_done.status != "done":
                raise HTTPException(500, f"Source refresh failed on node '{tnode.name}': {refresh_done.error_message}")
            pm._push_line(app_id, f"[Rolling] Source refreshed on node '{tnode.name}'.")

    has_custom = bool(app.nginx_enabled and app.domain)
    ssl_cert = ssl_key = None
    if has_custom:
        ssl_cert, ssl_key = _resolve_ssl_paths(app.ssl_cert_path, app.ssl_key_path)

    def _write_nginx_backends(backends: list[str]) -> None:
        cfg = nm.generate_config(
            app.name, app.domain if has_custom else None, backends,
            ssl_cert, ssl_key,
            app_id=app_id, mode="normal",
            extra_domains=json.loads(app.extra_domains or "[]") if has_custom else [],
            redirect_domains=json.loads(app.redirect_domains or "[]") if has_custom else [],
        )
        ok, msg = nm.write_nginx_config(app.name, cfg)
        if not ok:
            log.warning("[Rolling] nginx write failed: %s", msg)

    # Collect current backends so we can remove/add one at a time
    current_backends: list[str] = []
    for replica in running_replicas:
        tnode = node_map.get(replica.node_id) if replica.node_id else local_node
        if tnode is None:
            continue
        if tnode.is_local:
            if replica.external_port:
                current_backends.append(f"127.0.0.1:{replica.external_port}")
        else:
            if replica.tunnel_port:
                current_backends.append(f"127.0.0.1:{replica.tunnel_port}")

    completed_new_ids: set[int] = set()

    for idx, old_replica in enumerate(running_replicas, 1):
        tnode = node_map.get(old_replica.node_id) if old_replica.node_id else local_node
        if tnode is None:
            pm._push_line(app_id, f"[Rolling] Replica {old_replica.id}: node not found, skipping.")
            continue

        pm._push_line(app_id, f"[Rolling] Replacing replica {idx}/{len(running_replicas)} (id={old_replica.id})…")

        # Start new replacement replica
        new_ext_port = await _assign_external_port(None, tnode.id, None, db)
        new_replica = ApplicationReplica(
            app_id=app_id,
            node_id=tnode.id,
            external_port=new_ext_port,
            status="starting",
        )
        db.add(new_replica)
        await db.flush()
        await db.commit()

        if tnode.is_local:
            pm._push_line(app_id, f"[Rolling] Starting new instance (id={new_replica.id}) on port {new_ext_port}…")
            try:
                cid = await _start_instance_local(app, new_replica, env_vars, app_id)
                new_replica.container_id = cid
                await db.commit()
            except Exception as e:
                pm._push_line(app_id, f"[Rolling] Failed to start new instance: {e} — aborting roll.")
                await _zd_rollback(app_id, local_node.id, [(tnode, new_replica.id)], db)
                raise HTTPException(500, f"Rolling deploy aborted: {e}")

            # Health check new instance
            pm._push_line(app_id, f"[Rolling] Health checking new instance {new_replica.id} on port {new_ext_port}…")
            healthy, reason = await _wait_new_instance_healthy(app_id, new_replica.id, new_ext_port, is_local=True)

            if not healthy:
                pm._push_line(app_id, f"[Rolling] New instance {new_replica.id} failed health check ({reason}) — aborting roll.")
                await _zd_rollback(app_id, local_node.id, [(tnode, new_replica.id)], db)
                raise HTTPException(502, f"Rolling deploy aborted: instance {new_replica.id} failed health check: {reason}")

            async with AsyncSessionLocal() as _upd:
                r = await _upd.get(ApplicationReplica, new_replica.id)
                if r:
                    r.status = "running"
                    await _upd.commit()

            new_backend = f"127.0.0.1:{new_ext_port}"
            old_backend = f"127.0.0.1:{old_replica.external_port}" if old_replica.external_port else None

        else:
            if tnode.status != "online" or not tnode.enabled:
                pm._push_line(app_id, f"[Rolling] Node '{tnode.name}' offline — skipping replica {old_replica.id}.")
                continue

            remote_payload = _remote_replica_command_payload(app, env_vars, new_ext_port, source_revision)
            await queue_node_command(
                db, node_id=tnode.id, app_id=app_id,
                command_type="start_replica",
                payload={**remote_payload, "replica_id": new_replica.id},
            )
            await db.commit()

            # Wait for tunnel
            pm._push_line(app_id, f"[Rolling] Waiting for tunnel from '{tnode.name}' instance {new_replica.id} (max 120s)…")
            deadline = asyncio.get_running_loop().time() + 120
            tunnel_port = None
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(3)
                async with AsyncSessionLocal() as _poll_db:
                    fresh_r = await _poll_db.get(ApplicationReplica, new_replica.id)
                if fresh_r is None:
                    break
                if fresh_r.status == "error":
                    pm._push_line(app_id, f"[Rolling] Remote instance {new_replica.id} failed — aborting roll.")
                    await _zd_rollback(app_id, local_node.id, [(tnode, new_replica.id)], db)
                    raise HTTPException(500, f"Rolling deploy aborted: remote instance {new_replica.id} failed")
                if fresh_r.tunnel_port:
                    tunnel_port = fresh_r.tunnel_port
                    break

            if not tunnel_port:
                pm._push_line(app_id, f"[Rolling] Tunnel for instance {new_replica.id} did not connect — aborting roll.")
                await _zd_rollback(app_id, local_node.id, [(tnode, new_replica.id)], db)
                raise HTTPException(502, f"Rolling deploy aborted: tunnel for instance {new_replica.id} did not connect within 120s")

            pm._push_line(app_id, f"[Rolling] Health checking remote instance {new_replica.id} on tunnel port {tunnel_port}…")
            healthy, reason = await _wait_new_instance_healthy(app_id, new_replica.id, tunnel_port, is_local=False)

            if not healthy:
                pm._push_line(app_id, f"[Rolling] Remote instance {new_replica.id} failed health check ({reason}) — aborting roll.")
                await _zd_rollback(app_id, local_node.id, [(tnode, new_replica.id)], db)
                raise HTTPException(502, f"Rolling deploy aborted: remote instance {new_replica.id} failed health check: {reason}")

            async with AsyncSessionLocal() as _upd:
                r = await _upd.get(ApplicationReplica, new_replica.id)
                if r:
                    r.status = "running"
                    await _upd.commit()

            new_backend = f"127.0.0.1:{tunnel_port}"
            old_backend = f"127.0.0.1:{old_replica.tunnel_port}" if old_replica.tunnel_port else None

        # Swap nginx: add new, remove old
        if old_backend and old_backend in current_backends:
            current_backends.remove(old_backend)
        current_backends.append(new_backend)
        _write_nginx_backends(current_backends)
        pm._push_line(app_id, f"[Rolling] Instance {new_replica.id} live — nginx updated.")

        # Stop old replica
        if tnode.is_local:
            await asyncio.to_thread(dm.stop_replica_container, app_id, old_replica.id)
        else:
            await queue_node_command(
                db, node_id=tnode.id, app_id=app_id,
                command_type="stop_replica",
                payload={"app_id": app_id, "replica_id": old_replica.id, "app_name": app.name},
            )
        async with AsyncSessionLocal() as _del_db:
            victim = await _del_db.get(ApplicationReplica, old_replica.id)
            if victim:
                await _del_db.delete(victim)
                await _del_db.commit()

        completed_new_ids.add(new_replica.id)
        pm._push_line(app_id, f"[Rolling] Old instance {old_replica.id} stopped.")

    app.status = "running"
    if new_img:
        app.docker_image = new_img
    else:
        app.docker_image = dm.image_name(app_id, app.name)
    if source_revision:
        app.image_revision = source_revision

    await log_audit(db, "app.rolling_deploy", actor=actor, app_id=app_id, detail={"name": app.name})
    await db.commit()

    pm._push_line(app_id, f"[Rolling] Rolling deploy complete. {len(completed_new_ids)} replica(s) replaced.")
    return {"status": "ok", "image": app.docker_image, "replaced": len(completed_new_ids)}


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
    source_revision = _refresh_app_source_revision(app)

    # ── Step 1: Refresh / build image on every target node ───────────────────
    new_img: str | None = None
    for tnode in target_nodes:
        if tnode.is_local:
            if not app.working_dir:
                raise HTTPException(400, "No working directory — deploy the app first")
            try:
                def _push(_aid, line):
                    pm._push_line(app_id, str(line))
                new_img = await asyncio.to_thread(
                    dm.build_image,
                    app_id, app.name, app.working_dir, _push,
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
            pm._push_line(app_id, f"[ZD] Refreshing source on node '{tnode.name}'…")
            refresh_cmd = await queue_node_command(
                db, node_id=tnode.id, app_id=app_id,
                command_type="refresh_source",
                payload={
                    "app_name": app.name,
                    "app_id": app_id,
                    "app_type": app.app_type or "unknown",
                    "start_command": app.start_command or "",
                    "build_command": app.build_command or "",
                    "internal_port": _image_port(app),
                    "env_vars": env_vars,
                    "docker_options": _docker_runtime_options(app),
                    "source_revision": source_revision,
                },
            )
            refresh_done = await wait_for_node_command(db, refresh_cmd.id, timeout_seconds=300)
            if refresh_done.status != "done":
                raise HTTPException(500, f"Source refresh failed on node '{tnode.name}': {refresh_done.error_message}")
            pm._push_line(app_id, f"[ZD] Source refreshed on node '{tnode.name}'.")

    # ── Step 2: Create new replica rows and start containers on each slot ────
    # new_entries: list of (node, replica_id) — kept for rollback and nginx assembly
    new_entries: list[tuple[Node, int]] = []

    for tnode in target_slots:
        new_ext_port = await _assign_external_port(None, tnode.id, None, db)
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

        if tnode.is_local:
            pm._push_line(app_id, f"[ZD] Starting new local instance (id={new_replica.id}) on port {new_ext_port}…")
            try:
                cid = await _start_instance_local(app, new_replica, env_vars, app_id)
                new_replica.container_id = cid
                await db.commit()
            except Exception as e:
                await _zd_rollback(app_id, local_node.id, new_entries, db)
                raise HTTPException(500, f"Failed to start new local instance: {e}")
        else:
            pm._push_line(app_id, f"[ZD] Queuing start on node '{tnode.name}' for instance {new_replica.id}…")
            remote_payload = _remote_replica_command_payload(app, env_vars, new_ext_port)
            await queue_node_command(
                db, node_id=tnode.id, app_id=app_id,
                command_type="start_replica",
                payload={**remote_payload, "replica_id": new_replica.id},
            )
            await db.commit()

    # ── Step 3: Health-check every new replica ────────────────────────────────
    for tnode, new_rid in new_entries:
        if tnode.is_local:
            async with AsyncSessionLocal() as _poll_db:
                r = await _poll_db.get(ApplicationReplica, new_rid)
                check_port = r.external_port if r else None
            label = f"local port {check_port}"
        else:
            # Poll until the agent establishes the reverse tunnel (max 120 s)
            pm._push_line(app_id, f"[ZD] Waiting for tunnel from '{tnode.name}' instance {new_rid} (max 120s)…")
            deadline = asyncio.get_running_loop().time() + 120
            tunnel_port = None
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(3)
                async with AsyncSessionLocal() as _poll_db:
                    r = await _poll_db.execute(
                        select(ApplicationReplica).where(ApplicationReplica.id == new_rid)
                    )
                    fresh = r.scalar_one_or_none()
                if fresh is None:
                    break
                if fresh.status == "error":
                    await _zd_rollback(app_id, local_node.id, new_entries, db)
                    raise HTTPException(500, f"Instance {new_rid} on '{tnode.name}' failed: {fresh.last_error}")
                if fresh.tunnel_port:
                    tunnel_port = fresh.tunnel_port
                    break

            if not tunnel_port:
                await _zd_rollback(app_id, local_node.id, new_entries, db)
                raise HTTPException(502, f"Tunnel for instance {new_rid} on '{tnode.name}' did not connect within 120s — rolled back")

            check_port = tunnel_port
            label = f"tunnel port {check_port} (node '{tnode.name}')"

        pm._push_line(app_id, f"[ZD] Health checking instance {new_rid} on {label}…")
        healthy, reason = await _wait_new_instance_healthy(app_id, new_rid, check_port, is_local=tnode.is_local)

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
                if tnode.is_local:
                    if r.external_port:
                        new_backends.append(f"127.0.0.1:{r.external_port}")
                else:
                    if r.tunnel_port:
                        new_backends.append(f"127.0.0.1:{r.tunnel_port}")
        new_ids.add(new_rid)

    app.status = "running"
    if new_img:
        app.docker_image = new_img
    else:
        app.docker_image = dm.image_name(app_id, app.name)
    if source_revision:
        app.image_revision = source_revision
    await db.flush()

    has_custom = bool(app.nginx_enabled and app.domain)
    ssl_cert = ssl_key = None
    if has_custom:
        ssl_cert, ssl_key = _resolve_ssl_paths(app.ssl_cert_path, app.ssl_key_path)
    cfg = nm.generate_config(
        app.name, app.domain if has_custom else None, new_backends,
        ssl_cert, ssl_key,
        app_id=app_id, mode="normal",
        extra_domains=json.loads(app.extra_domains or "[]") if has_custom else [],
        redirect_domains=json.loads(app.redirect_domains or "[]") if has_custom else [],
    )
    ok, msg = nm.write_nginx_config(app.name, cfg)
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
        if old_r.node_id is None or old_r.node_id == local_node.id:
            await asyncio.to_thread(dm.stop_replica_container, app_id, old_r.id)
            await db.delete(old_r)
            pm._push_line(app_id, f"[ZD] Stopped old local instance {old_r.id}.")
        else:
            await queue_node_command(
                db, node_id=old_r.node_id, app_id=app_id,
                command_type="stop_replica",
                payload={"app_id": app_id, "replica_id": old_r.id, "app_name": app.name},
            )
            await db.delete(old_r)
            pm._push_line(app_id, f"[ZD] Queued stop for old remote instance {old_r.id}.")

    first_new_id = new_entries[0][1] if new_entries else None
    await log_audit(db, "app.zero_downtime_deploy", actor=actor, app_id=app_id, detail={"name": app.name})
    await db.commit()

    pm._push_line(app_id, f"[ZD] Zero-downtime deploy complete. New instance(s): {[nid for _, nid in new_entries]}.")
    return {"status": "ok", "image": app.docker_image, "instance_id": first_new_id}


async def _wait_new_instance_healthy(app_id: int, replica_id: int, port: int, is_local: bool) -> tuple[bool, str]:
    """Poll a freshly started deploy instance until it serves HTTP.

    Returns (healthy, reason). Gives up early when the container has exited
    (local) or the node reported the replica as failed (remote).
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = started + DEPLOY_HEALTH_TIMEOUT_SECONDS
    next_progress = started + DEPLOY_HEALTH_PROGRESS_SECONDS
    while loop.time() < deadline:
        if await asyncio.to_thread(_local_http_service_ready, port):
            return True, ""
        if is_local:
            if not await asyncio.to_thread(dm.is_replica_container_running, app_id, replica_id):
                return False, "container is no longer running (check the instance logs)"
        else:
            async with AsyncSessionLocal() as _poll_db:
                r = await _poll_db.get(ApplicationReplica, replica_id)
            if r is None:
                return False, "instance was removed"
            if r.status == "error":
                return False, f"node reported an error: {r.last_error or 'unknown'}"
        if loop.time() >= next_progress:
            pm._push_line(app_id, f"[Deploy] Instance {replica_id} not responding yet after {int(loop.time() - started)}s — still waiting (max {DEPLOY_HEALTH_TIMEOUT_SECONDS}s)…")
            next_progress += DEPLOY_HEALTH_PROGRESS_SECONDS
        await asyncio.sleep(2)
    return False, f"no healthy HTTP response within {DEPLOY_HEALTH_TIMEOUT_SECONDS}s"


async def _zd_rollback(app_id: int, local_node_id: int, new_entries: list[tuple["Node", int]], db: "AsyncSession") -> None:
    """Best-effort cleanup of new replicas created during a failed ZD deploy."""
    for tnode, new_rid in new_entries:
        try:
            async with AsyncSessionLocal() as _rb_db:
                victim = await _rb_db.get(ApplicationReplica, new_rid)
                if victim:
                    if tnode.is_local:
                        await asyncio.to_thread(dm.stop_replica_container, app_id, new_rid)
                    else:
                        await queue_node_command(
                            _rb_db, node_id=tnode.id, app_id=app_id,
                            command_type="stop_replica",
                            payload={"app_id": app_id, "replica_id": new_rid, "app_name": victim.app_id},
                        )
                    await _rb_db.delete(victim)
                    await _rb_db.commit()
        except Exception as _e:
            log.warning("[ZD rollback] failed to clean up replica %d: %s", new_rid, _e)
