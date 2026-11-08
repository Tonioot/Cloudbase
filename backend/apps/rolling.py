import asyncio
import json
import logging
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import AsyncSessionLocal
from core.models import Node, ApplicationReplica
from containers import processes as pm, naming as containers_naming, build as containers_build
from nginx import config as nginx_config, files as nginx_files
from nodes.registry import ensure_local_node
from nodes import desired
from core.env_crypto import decrypt_env
from core.audit import log_audit
from apps.git import _refresh_app_source_revision
from apps.images import _image_build_kwargs, _image_port
from apps.nginx_sync import _resolve_ssl_paths, _has_public_nginx_domain
from apps.runtime import _start_replica_runtime
from apps.serializers import _get_or_404
from apps.validation import _assign_external_port
from apps.bluegreen import _do_zero_downtime_deploy, _zd_rollback
from apps.deploy_common import REMOTE_START_TIMEOUT_SECONDS, _wait_new_instance_healthy
from core import activity
from mesh import routing

log = logging.getLogger("cloudbase.apps")


@activity.tracked("deploying")
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
            pm._push_line(app_id, "[Rolling] Checking the image…")
            try:
                def _push(_aid, line):
                    pm._push_line(app_id, str(line))
                new_img, _built = await asyncio.to_thread(
                    containers_build.build_image_unless_current,
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
            # The node downloads the source and builds the image by itself when it starts the new instance
            if tnode.status != "online":
                raise HTTPException(400, f"Node '{tnode.name}' is offline")

    has_custom = bool(app.nginx_enabled and app.domain)
    ssl_cert = ssl_key = None
    if has_custom:
        ssl_cert, ssl_key = _resolve_ssl_paths(app.ssl_cert_path, app.ssl_key_path)

    def _write_nginx_backends(backends: list[str]) -> None:
        cfg = nginx_config.generate_config(
            app.name, app.domain if has_custom else None, backends,
            ssl_cert, ssl_key,
            app_id=app_id, mode="normal",
            extra_domains=json.loads(app.extra_domains or "[]") if has_custom else [],
            redirect_domains=json.loads(app.redirect_domains or "[]") if has_custom else [],
        )
        ok, msg = nginx_files.write_nginx_config(app.name, cfg)
        if not ok:
            log.warning("[Rolling] nginx write failed: %s", msg)

    # Collect current backends so we can remove/add one at a time
    current_backends: list[str] = []
    for replica in running_replicas:
        tnode = node_map.get(replica.node_id) if replica.node_id else local_node
        if tnode is None:
            continue
        backend = routing.backend_for(replica, tnode)
        if backend:
            current_backends.append(backend)

    completed_new_ids: set[int] = set()

    for idx, old_replica in enumerate(running_replicas, 1):
        tnode = node_map.get(old_replica.node_id) if old_replica.node_id else local_node
        if tnode is None:
            pm._push_line(app_id, f"[Rolling] Replica {old_replica.id}: node not found, skipping.")
            continue

        pm._push_line(app_id, f"[Rolling] Replacing replica {idx}/{len(running_replicas)} (id={old_replica.id})…")

        if not tnode.is_local and (tnode.status != "online" or not tnode.enabled):
            pm._push_line(app_id, f"[Rolling] Node '{tnode.name}' offline — skipping replica {old_replica.id}.")
            continue

        # Start new replacement replica
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

        pm._push_line(app_id, f"[Rolling] Starting new instance {new_replica.id} on '{tnode.name}' (port {new_ext_port})…")
        await _start_replica_runtime(app, new_replica, tnode, env_vars, db, source_revision)
        await db.commit()

        pm._push_line(app_id, f"[Rolling] Waiting for instance {new_replica.id} to be built and reachable (max {REMOTE_START_TIMEOUT_SECONDS // 60} min)…")
        try:
            new_backend = await routing.wait_reachable(new_replica.id, REMOTE_START_TIMEOUT_SECONDS)
        except routing.Unreachable as exc:
            pm._push_line(app_id, f"[Rolling] Instance {new_replica.id}: {exc} — aborting roll.")
            await _zd_rollback(app_id, local_node.id, [(tnode, new_replica.id)], db)
            raise HTTPException(502, f"Rolling deploy aborted: instance {new_replica.id} on '{tnode.name}': {exc}")

        pm._push_line(app_id, f"[Rolling] Health checking instance {new_replica.id} on {new_backend}…")
        healthy, reason = await _wait_new_instance_healthy(app_id, new_replica.id, new_backend)

        if not healthy:
            pm._push_line(app_id, f"[Rolling] Instance {new_replica.id} failed health check ({reason}) — aborting roll.")
            await _zd_rollback(app_id, local_node.id, [(tnode, new_replica.id)], db)
            raise HTTPException(502, f"Rolling deploy aborted: instance {new_replica.id} failed health check: {reason}")

        async with AsyncSessionLocal() as _upd:
            r = await _upd.get(ApplicationReplica, new_replica.id)
            if r:
                r.status = "running"
                await _upd.commit()

        old_backend = routing.backend_for(old_replica, tnode)

        # Swap nginx: add new, remove old
        if old_backend and old_backend in current_backends:
            current_backends.remove(old_backend)
        current_backends.append(new_backend)
        _write_nginx_backends(current_backends)
        pm._push_line(app_id, f"[Rolling] Instance {new_replica.id} live — nginx updated.")

        # Stop old replica: it leaves the list below and its node (this server too) removes the container
        desired.nudge(tnode.id)
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
        app.docker_image = containers_naming.image_name(app_id, app.name)
    if source_revision:
        app.image_revision = source_revision

    await log_audit(db, "app.rolling_deploy", actor=actor, app_id=app_id, detail={"name": app.name})
    await db.commit()

    pm._push_line(app_id, f"[Rolling] Rolling deploy complete. {len(completed_new_ids)} replica(s) replaced.")
    return {"status": "ok", "image": app.docker_image, "replaced": len(completed_new_ids)}
