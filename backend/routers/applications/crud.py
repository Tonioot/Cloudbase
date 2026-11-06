import asyncio
import json
import logging
import os
import re
import shutil
from fastapi import Request, HTTPException, Query, Depends, BackgroundTasks, APIRouter
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import get_db
import datetime as _dt
from core.models import StatsHistory, Application, ApplicationReplica, Node
from containers import processes as pm, manager as dm
from nginx import manager as nm, certificates as certs
from cron import scheduler as _cron
from nodes.registry import ensure_local_node
from nodes.commands import queue_node_command, wait_for_node_command
from core.env_crypto import encrypt_env, decrypt_env
from core.audit import log_audit
from core import auth as _auth, config as _cfg, system_config as _syscfg
from apps.git import _deploy_app, _encrypt_github_token, _normalize_repo_url, _resolve_token
from apps.images import _dockerfile_build_kwargs, _image_port
from apps.nginx_sync import _best_effort_delete_app_certificate, _best_effort_remove_app_nginx, _ensure_maintenance_files, _get_nginx_backends, _get_nginx_mode, _resolve_ssl_paths, _write_app_nginx_config
from apps.runtime import _best_effort_stop_legacy_app_container, _cleanup_app_dependencies, _derive_app_status_from_instances, _get_app_node, _load_node_map, _sync_process_status
from apps.serializers import _app_to_dict, _get_or_404, _replica_to_dict
from apps.validation import _assign_external_port, _check_domain_conflicts, _check_new_app_name, _check_repo_url, _validate_docker_runtime_settings
from routers.applications.deps import _request_can_manage_apps
from routers.applications.schemas import DeployRequest, ExportRequest, ImportRequest, UpdateRequest

router = APIRouter(prefix="/api/apps", tags=["applications"])


log = logging.getLogger("cloudbase.apps")


@router.get("")
async def list_apps(request: Request, db: AsyncSession = Depends(get_db), _viewer: dict = Depends(_auth.require_permission("apps.view"))):
    await ensure_local_node(db)
    result = await db.execute(select(Application))
    apps = result.scalars().all()
    node_map = await _load_node_map(db)

    # Batch-fetch all replica rows so we can derive statuses without N+1 Docker checks
    rep_result = await db.execute(select(ApplicationReplica))
    all_replicas = rep_result.scalars().all()
    replica_map: dict[int, list] = {}
    for r in all_replicas:
        replica_map.setdefault(r.app_id, []).append(r)

    # Derive status from replica statuses — no live Docker check needed
    async def _check(app):
        app_replicas = replica_map.get(app.id)
        if app_replicas:
            return app.id, _derive_app_status_from_instances(app_replicas), None
        return app.id, app.status, app.pid

    checks = await asyncio.gather(*[_check(a) for a in apps])

    id_map = {a.id: a for a in apps}
    dirty = False
    for app_id, new_status, new_pid in checks:
        a = id_map[app_id]
        if a.status != new_status or a.pid != new_pid:
            a.status = new_status
            a.pid = new_pid
            dirty = True
    if dirty:
        await db.commit()

    include_sensitive = await _request_can_manage_apps(request)
    result_list = []
    for a in apps:
        app_replicas = replica_map.get(a.id, [])
        replica_dicts = [_replica_to_dict(r, node_map.get(r.node_id)) for r in app_replicas]
        result_list.append(
            _app_to_dict(
                a,
                include_sensitive=include_sensitive,
                replicas=replica_dicts,
                include_page_configs=False,
            )
        )
    return result_list


@router.post("")
async def deploy_app(
    req: DeployRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("apps.deploy")),
    actor: str = Depends(_auth.get_current_actor),
):
    existing = await db.execute(select(Application).where(Application.name == req.name))
    if existing.scalar_one_or_none():
        raise HTTPException(400, f"App '{req.name}' already exists")
    req.name = await _check_new_app_name(req.name, db)
    req.repo_url = _check_repo_url(_normalize_repo_url(req.repo_url))

    all_domains = [req.domain] + (req.extra_domains or []) + (req.redirect_domains or [])
    await _check_domain_conflicts([d for d in all_domains if d], db)

    app_count_result = await db.execute(select(func.count()).select_from(Application))
    if app_count_result.scalar() >= _cfg.get_limit("max_apps"):
        raise HTTPException(400, f"App limit reached ({_cfg.get_limit('max_apps')} apps maximum). Adjust limits.max_apps in config.yaml to increase.")

    if req.restart_policy is not None and req.restart_policy not in ("no", "always", "on-failure"):
        raise HTTPException(400, "restart_policy must be one of: no, always, on-failure")
    _validate_docker_runtime_settings(req.docker_cpu_limit, req.docker_memory_limit_mb, req.docker_tmpfs_size_mb)

    local_node = await ensure_local_node(db)
    target_node = local_node
    if req.node_id is not None:
        node_result = await db.execute(select(Node).where(Node.id == req.node_id, Node.enabled == True))
        target_node = node_result.scalar_one_or_none()
        if not target_node:
            raise HTTPException(400, "Selected node is not available")

    is_static = (req.app_type or "").strip().lower() == "static"
    is_cron = bool((req.cron_schedule or "").strip())
    if is_cron:
        if is_static:
            raise HTTPException(400, "A static site can't be a cron job")
        _cron.check_schedule(req.cron_schedule, req.cron_timezone)
        if req.cron_timeout_seconds is not None and req.cron_timeout_seconds not in _cron.TIMEOUT_CHOICES:
            raise HTTPException(400, f"cron_timeout_seconds must be one of {_cron.TIMEOUT_CHOICES}")
    if is_static:
        try:
            dm.resolve_static_dir("", req.start_command or "")
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    # no_web apps (discord bots, background workers, etc.) don't need a port or nginx
    is_no_web = (bool(req.no_web) or is_cron) and not is_static
    node_id_for_port = target_node.id if target_node else local_node.id
    external_port = None if is_no_web else await _assign_external_port(req.external_port, node_id_for_port, None, db)

    app = Application(
        name=req.name,
        repo_url=req.repo_url,
        github_token=_encrypt_github_token(_resolve_token(req.github_token, req.github_token_id)),
        github_token_id=req.github_token_id or None,
        domain=req.domain,
        extra_domains=json.dumps(req.extra_domains or []),
        redirect_domains=json.dumps(req.redirect_domains or []),
        ssl_cert_path=req.ssl_cert_path,
        ssl_key_path=req.ssl_key_path,
        start_command=req.start_command,
        build_command=(req.build_command or "").strip() or None,
        port=dm.STATIC_INTERNAL_PORT if is_static else req.port,
        app_type="static" if is_static else None,
        external_port=external_port,
        env_vars=encrypt_env(req.env_vars or {}),
        auto_start=bool(req.auto_start) if req.auto_start is not None else False,
        restart_policy=req.restart_policy or "no",
        no_web=is_no_web,
        docker_cpu_limit=req.docker_cpu_limit,
        docker_memory_limit_mb=req.docker_memory_limit_mb,
        docker_read_only_root=bool(req.docker_read_only_root) if req.docker_read_only_root is not None else False,
        docker_tmpfs_enabled=bool(req.docker_tmpfs_enabled) if req.docker_tmpfs_enabled is not None else False,
        docker_tmpfs_size_mb=req.docker_tmpfs_size_mb,
        source_revision=req.source_revision,
        image_revision=req.image_revision,
        status="deploying",
        node_id=target_node.id if not target_node.is_local else None,
        cron_schedule=" ".join(req.cron_schedule.split()) if is_cron else None,
        cron_timezone=(req.cron_timezone or None) if is_cron else None,
        cron_timeout_seconds=req.cron_timeout_seconds or 3600,
        cron_enabled=True,
    )
    db.add(app)
    await db.commit()
    await db.refresh(app)

    if not target_node.is_local and not is_cron:
        # Nginx runs on the primary and reaches remote instances through
        # tunnels, so a custom domain is routed here too. The config itself is
        # written once instances start (that path requires this flag).
        if app.domain and not app.no_web:
            app.nginx_enabled = True
        await db.commit()
        await db.refresh(app)
        return _app_to_dict(app)

    try:
        await _deploy_app(app)
        app.status = _cron.status_of(app) if is_cron else "stopped"

        if app.domain and not app.no_web:
            app.nginx_enabled = True
            await _write_app_nginx_config(app, db, local_node)

        await log_audit(db, "app.deploy", actor=actor, app_id=app.id, detail={"name": app.name})
        await db.commit()
        await db.refresh(app)
        return _app_to_dict(app)
    except Exception as e:
        app.status = "error"
        await db.commit()
        raise HTTPException(500, str(e))


@router.get("/{app_id}")
async def get_app(app_id: int, request: Request, _user: dict = Depends(_auth.require_permission("apps.view")), db: AsyncSession = Depends(get_db)):
    await ensure_local_node(db)
    app = await _get_or_404(app_id, db)
    await _sync_process_status(app, db)
    node_map = await _load_node_map(db)
    replica_result = await db.execute(
        select(ApplicationReplica).where(ApplicationReplica.app_id == app_id)
    )
    replicas = replica_result.scalars().all()
    replicas_dicts = [_replica_to_dict(r, node_map.get(r.node_id)) for r in replicas]
    return _app_to_dict(app, include_sensitive=await _request_can_manage_apps(request), replicas=replicas_dicts)


@router.get("/{app_id}/stats/history")
async def get_stats_history(
    app_id: int,
    hours: int = Query(24, ge=1, le=168),
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(_auth.require_permission("apps.view")),
):
    since = _dt.datetime.utcnow() - _dt.timedelta(hours=hours)
    result = await db.execute(
        select(StatsHistory)
        .where(StatsHistory.app_id == app_id, StatsHistory.timestamp >= since)
        .order_by(StatsHistory.timestamp.asc())
    )
    rows = result.scalars().all()
    return [
        {
            "timestamp":   r.timestamp.isoformat() + "Z",
            "cpu_percent": r.cpu_percent,
            "memory_mb":   r.memory_mb,
            "net_mb":      r.net_mb  or 0,
            "disk_mb":     r.disk_mb or 0,
        }
        for r in rows
    ]


@router.put("/{app_id}")
async def update_app(app_id: int, req: UpdateRequest, db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("apps.configure")), actor: str = Depends(_auth.get_current_actor)):
    app = await _get_or_404(app_id, db)
    await ensure_local_node(db)
    _validate_docker_runtime_settings(req.docker_cpu_limit, req.docker_memory_limit_mb, req.docker_tmpfs_size_mb)
    dockerfile_changed = False

    if req.domain is not None or req.extra_domains is not None or req.redirect_domains is not None:
        new_primary   = req.domain        if req.domain        is not None else app.domain
        new_extra     = req.extra_domains if req.extra_domains is not None else json.loads(app.extra_domains    or "[]")
        new_redirects = req.redirect_domains if req.redirect_domains is not None else json.loads(app.redirect_domains or "[]")
        all_domains = [new_primary] + (new_extra or []) + (new_redirects or [])
        await _check_domain_conflicts([d for d in all_domains if d], db, exclude_app_id=app.id)
        await certs.forget_pending([d for d in all_domains if d])

    if req.domain is not None:
        app.domain = req.domain
    if req.extra_domains is not None:
        app.extra_domains = json.dumps(req.extra_domains)
    if req.redirect_domains is not None:
        app.redirect_domains = json.dumps(req.redirect_domains)
    if req.ssl_cert_path is not None:
        app.ssl_cert_path = req.ssl_cert_path
    if req.ssl_key_path is not None:
        app.ssl_key_path = req.ssl_key_path
    if req.start_command is not None:
        if req.start_command != app.start_command:
            dockerfile_changed = True
        if app.app_type == "static":
            try:
                dm.resolve_static_dir("", req.start_command)
            except ValueError as exc:
                raise HTTPException(400, str(exc))
        app.start_command = req.start_command
        if app.app_type != "static":
            app.app_type = pm.detect_app_type_from_command(req.start_command)
    if req.build_command is not None:
        new_build = req.build_command.strip() or None
        if new_build != app.build_command:
            dockerfile_changed = True
        app.build_command = new_build
    if req.port is not None:
        if req.port != app.port:
            dockerfile_changed = True
        app.port = req.port
    if req.external_port is not None:
        app.external_port = await _assign_external_port(req.external_port, app.node_id, app.id, db)
    if req.docker_cpu_limit is not None:
        app.docker_cpu_limit = req.docker_cpu_limit
    if req.docker_memory_limit_mb is not None:
        app.docker_memory_limit_mb = req.docker_memory_limit_mb
    if req.docker_read_only_root is not None:
        app.docker_read_only_root = req.docker_read_only_root
    if req.docker_tmpfs_enabled is not None:
        app.docker_tmpfs_enabled = req.docker_tmpfs_enabled
    if req.docker_tmpfs_size_mb is not None:
        app.docker_tmpfs_size_mb = req.docker_tmpfs_size_mb
    if req.env_vars is not None or req.env_var_keys is not None:
        existing = decrypt_env(app.env_vars or "")
        if req.env_var_keys is not None:
            # Keep only keys that are still present in the UI
            existing = {k: v for k, v in existing.items() if k in req.env_var_keys}
        if req.env_vars:
            # Merge in new/changed values
            existing.update(req.env_vars)
        new_env = encrypt_env(existing)
        # The build step reads env vars (e.g. NEXT_PUBLIC_*), so a change
        # must produce a fresh image rather than reuse the cached one.
        if app.build_command and existing != decrypt_env(app.env_vars or ""):
            dockerfile_changed = True
        app.env_vars = new_env
    resolved = _resolve_token(req.github_token, req.github_token_id)
    if resolved is not None:
        app.github_token = _encrypt_github_token(resolved)
        app.github_token_id = req.github_token_id or None
    if req.auto_start is not None:
        app.auto_start = req.auto_start
    if req.restart_policy is not None and req.restart_policy in ("no", "always", "on-failure"):
        app.restart_policy = req.restart_policy
    if req.source_revision is not None:
        app.source_revision = req.source_revision
    if req.image_revision is not None:
        app.image_revision = req.image_revision
    if req.auto_deploy is not None:
        app.auto_deploy = req.auto_deploy
    if req.deploy_branch is not None:
        branch = req.deploy_branch.strip()
        if branch and (branch.startswith("-") or ".." in branch or not re.match(r"^[A-Za-z0-9._/-]{1,200}$", branch)):
            raise HTTPException(400, "Invalid branch name")
        app.deploy_branch = branch or None
    if req.deploy_strategy is not None:
        if req.deploy_strategy not in ("rolling", "blue_green"):
            raise HTTPException(400, "deploy_strategy must be rolling or blue_green")
        app.deploy_strategy = req.deploy_strategy
    if req.auto_deploy_interval is not None:
        app.auto_deploy_interval = max(30, min(int(req.auto_deploy_interval), 3600))
    if req.autoscale_enabled is not None:
        app.autoscale_enabled = req.autoscale_enabled
    if req.autoscale_min_replicas is not None:
        app.autoscale_min_replicas = max(1, req.autoscale_min_replicas)
    if req.autoscale_max_replicas is not None:
        app.autoscale_max_replicas = max(1, req.autoscale_max_replicas)
    if req.autoscale_cpu_target is not None:
        app.autoscale_cpu_target = max(10.0, min(95.0, req.autoscale_cpu_target))

    if dockerfile_changed:
        if app.working_dir and os.path.exists(app.working_dir):
            await asyncio.to_thread(
                dm.ensure_dockerfile,
                app.working_dir,
                app.app_type or "unknown",
                app.start_command or "",
                _image_port(app),
                **_dockerfile_build_kwargs(app),
            )
        # Force image refresh on next start/restart so config changes are applied.
        app.image_revision = None

    if app.domain and not app.no_web:
        maint_ok, maint_msg = _ensure_maintenance_files(app, app.id)
        if not maint_ok:
            raise HTTPException(500, f"Maintenance files failed: {maint_msg}")
        ssl_cert, ssl_key = _resolve_ssl_paths(app.ssl_cert_path, app.ssl_key_path)
        backends = await _get_nginx_backends(app, db)
        config = nm.generate_config(
            app.name, app.domain, backends,
            ssl_cert, ssl_key,
            app_id=app.id, mode=_get_nginx_mode(app),
            extra_domains=json.loads(app.extra_domains or "[]"),
            redirect_domains=json.loads(app.redirect_domains or "[]"),
        )
        ok, msg = nm.write_nginx_config(app.name, config)
        app.nginx_enabled = ok
        if not ok:
            raise HTTPException(500, f"Nginx config failed: {msg}")
    elif req.domain is not None and not req.domain.strip() and not app.no_web:
        # Every custom domain was removed: drop them from nginx and fall back to
        # the automatic subdomain (base domain) if there is one.
        app.domain = None
        app.extra_domains = "[]"
        app.redirect_domains = "[]"
        app.ssl_cert_path = None
        app.ssl_key_path = None
        app.nginx_enabled = False
        if _syscfg.get_base_domain_cached():
            await _write_app_nginx_config(app, db, await ensure_local_node(db))
        else:
            _best_effort_remove_app_nginx(app.name)

    await log_audit(db, "app.config_update", actor=actor, app_id=app.id, detail={"name": app.name})
    await db.commit()
    return _app_to_dict(app)


@router.delete("/{app_id}")
async def delete_app(app_id: int, db: AsyncSession = Depends(get_db), _user: dict = Depends(_auth.require_permission("apps.delete")), actor: str = Depends(_auth.get_current_actor)):
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)
    node = await _get_app_node(app, db, local_node)

    if not node.is_local:
        if node.status == "online":
            cmd = await queue_node_command(
                db,
                node_id=node.id,
                app_id=app.id,
                command_type="delete_app",
                payload={"app_id": app.id, "app_name": app.name},
            )
            done = await wait_for_node_command(db, cmd.id, timeout_seconds=60)
            if done.status != "done":
                raise HTTPException(500, f"Failed to delete app on node '{node.name}': {done.error_message}")
            _best_effort_remove_app_nginx(app.name)
            _best_effort_delete_app_certificate(app)
            await _cleanup_app_dependencies(db, app.id)
            await db.delete(app)
            try:
                await db.commit()
            except IntegrityError as e:
                await db.rollback()
                raise HTTPException(409, f"App '{app.name}' cannot be deleted because it is still referenced") from e
            result_payload = json.loads(done.result or "{}") if done.result else {}
            return {"message": result_payload.get("message") or f"App '{app.name}' deleted"}
        else:
            # Node offline — remove from DB only; node cleans up its own files when it reconnects
            _best_effort_remove_app_nginx(app.name)
            _best_effort_delete_app_certificate(app)
            await _cleanup_app_dependencies(db, app.id)
            await db.delete(app)
            try:
                await db.commit()
            except IntegrityError as e:
                await db.rollback()
                raise HTTPException(409, f"App '{app.name}' cannot be deleted because it is still referenced") from e
            return {"message": f"App '{app.name}' removed (node '{node.name}' was offline — app files may still exist on the node)"}

    # Stop and remove all replica containers before deleting
    replica_result = await db.execute(
        select(ApplicationReplica).where(ApplicationReplica.app_id == app_id)
    )
    node_map = await _load_node_map(db)
    for replica in replica_result.scalars().all():
        r_node = node_map.get(replica.node_id)
        if r_node and r_node.is_local:
            await asyncio.to_thread(pm.stop_docker_replica, app_id, replica.id)
        elif r_node and not r_node.is_local and r_node.status == "online":
            await queue_node_command(
                db, node_id=r_node.id, app_id=app_id,
                command_type="stop_replica",
                payload={"app_id": app_id, "replica_id": replica.id, "app_name": app.name},
            )

    await _best_effort_stop_legacy_app_container(app_id)
    await asyncio.to_thread(dm.remove_image, app_id, app.name)

    _best_effort_remove_app_nginx(app.name)
    _best_effort_delete_app_certificate(app)

    app_dir = pm.get_app_dir(app.name)
    if os.path.exists(app_dir):
        shutil.rmtree(app_dir)

    app_name = app.name
    app_id_val = app.id
    await _cleanup_app_dependencies(db, app.id)
    await db.delete(app)
    await log_audit(db, "app.delete", actor=actor, detail={"name": app_name, "app_id": app_id_val})
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(409, f"App '{app_name}' cannot be deleted because it is still referenced") from e
    return {"message": f"App '{app_name}' deleted"}


@router.post("/export")
async def export_apps(req: ExportRequest, _user: dict = Depends(_auth.require_permission("apps.configure")), db: AsyncSession = Depends(get_db)):
    query = select(Application)
    if req.app_ids:
        query = query.where(Application.id.in_(req.app_ids))
    result = await db.execute(query)
    apps = result.scalars().all()

    export_data = []
    for app in apps:
        export_data.append({
            "name": app.name,
            "repo_url": app.repo_url,
            "github_token": None,
            "domain": app.domain,
            "extra_domains": json.loads(app.extra_domains or "[]"),
            "redirect_domains": json.loads(app.redirect_domains or "[]"),
            "ssl_cert_path": app.ssl_cert_path,
            "ssl_key_path": app.ssl_key_path,
            "app_type": app.app_type,
            "start_command": app.start_command,
            "build_command": app.build_command,
            "port": app.port,
            "env_vars": {},
            "auto_start": app.auto_start,
            "restart_policy": app.restart_policy,
            "docker_cpu_limit": app.docker_cpu_limit,
            "docker_memory_limit_mb": app.docker_memory_limit_mb,
            "docker_read_only_root": bool(app.docker_read_only_root),
            "docker_tmpfs_enabled": bool(app.docker_tmpfs_enabled),
            "docker_tmpfs_size_mb": app.docker_tmpfs_size_mb,
            "source_revision": app.source_revision,
            "image_revision": app.image_revision,
            "external_port": None,
            "maintenance_mode": app.maintenance_mode,
            "update_mode": app.update_mode,
            "downtime_page": json.loads(app.downtime_page or "{}"),
            "update_page": json.loads(app.update_page or "{}"),
            "restart_page": json.loads(app.restart_page or "{}"),
            "starting_page": json.loads(app.starting_page or "{}"),
            "no_web": bool(app.no_web),
            "cron_schedule": app.cron_schedule,
            "cron_timezone": app.cron_timezone,
            "cron_enabled": bool(app.cron_enabled),
            "cron_timeout_seconds": app.cron_timeout_seconds,
        })

    return {"exported_apps": export_data}


@router.post("/import")
async def import_apps(req: ImportRequest, background_tasks: BackgroundTasks, _user: dict = Depends(_auth.require_permission("apps.configure")), db: AsyncSession = Depends(get_db)):
    local_node = await ensure_local_node(db)
    
    imported_count = 0
    for app_data in req.apps:
        existing = await db.execute(select(Application).where(Application.name == app_data.get("name")))
        if existing.scalar_one_or_none():
            continue  # Skip existing apps by name
        try:
            app_data["name"] = await _check_new_app_name(app_data.get("name"), db)
            app_data["repo_url"] = _check_repo_url(app_data.get("repo_url"))
        except HTTPException as e:
            log.warning("[import] skipped app %r: %s", app_data.get("name"), e.detail)
            continue

        target_node_id = req.target_node_id or app_data.get("node_id") or local_node.id
        node_result = await db.execute(select(Node).where(Node.id == target_node_id, Node.enabled == True))
        target_node = node_result.scalar_one_or_none()
        if not target_node:
            target_node = local_node

        # A cron job keeps its schedule when it is valid; otherwise it comes in as a worker
        cron_schedule = " ".join((app_data.get("cron_schedule") or "").split()) or None
        if cron_schedule:
            try:
                _cron.check_schedule(cron_schedule, app_data.get("cron_timezone"))
            except HTTPException as e:
                log.warning("[import] app %r: schedule dropped: %s", app_data.get("name"), e.detail)
                cron_schedule = None
        no_web = bool(app_data.get("no_web")) or bool(cron_schedule) or bool(app_data.get("cron_schedule"))
        import_external_port = None if no_web else await _assign_external_port(None, target_node.id, None, db)

        app = Application(
            name=app_data.get("name"),
            repo_url=app_data.get("repo_url"),
            github_token=_encrypt_github_token(app_data.get("github_token")),
            domain=app_data.get("domain"),
            extra_domains=json.dumps(app_data.get("extra_domains") or []),
            redirect_domains=json.dumps(app_data.get("redirect_domains") or []),
            ssl_cert_path=None,
            ssl_key_path=None,
            start_command=app_data.get("start_command"),
            build_command=app_data.get("build_command"),
            port=app_data.get("port"),
            app_type="static" if app_data.get("app_type") == "static" else None,
            external_port=import_external_port,
            env_vars=encrypt_env(app_data.get("env_vars") or {}),
            auto_start=bool(app_data.get("auto_start")) if app_data.get("auto_start") is not None else False,
            restart_policy=app_data.get("restart_policy") or "no",
                docker_cpu_limit=app_data.get("docker_cpu_limit"),
            docker_memory_limit_mb=app_data.get("docker_memory_limit_mb"),
            docker_read_only_root=bool(app_data.get("docker_read_only_root")) if app_data.get("docker_read_only_root") is not None else False,
            docker_tmpfs_enabled=bool(app_data.get("docker_tmpfs_enabled")) if app_data.get("docker_tmpfs_enabled") is not None else False,
            docker_tmpfs_size_mb=app_data.get("docker_tmpfs_size_mb"),
            source_revision=app_data.get("source_revision"),
            image_revision=app_data.get("image_revision"),
            status="deploying",
            node_id=target_node.id if not target_node.is_local else None,
            maintenance_mode=app_data.get("maintenance_mode", False),
            update_mode=app_data.get("update_mode", False),
            downtime_page=json.dumps(app_data.get("downtime_page") or {}),
            update_page=json.dumps(app_data.get("update_page") or {}),
            restart_page=json.dumps(app_data.get("restart_page") or {}),
            starting_page=json.dumps(app_data.get("starting_page") or {}),
            no_web=no_web,
            cron_schedule=cron_schedule,
            cron_timezone=(app_data.get("cron_timezone") or None) if cron_schedule else None,
            cron_enabled=bool(app_data.get("cron_enabled", True)),
            cron_timeout_seconds=(app_data.get("cron_timeout_seconds")
                                  if app_data.get("cron_timeout_seconds") in _cron.TIMEOUT_CHOICES else 3600),
        )
        db.add(app)
        await db.commit()
        await db.refresh(app)

        if target_node.is_local or app.cron_schedule:  # cron jobs are built on the primary
            try:
                await _deploy_app(app)
                app.status = _cron.status_of(app) if app.cron_schedule else "stopped"
                if app.domain and not app.no_web:
                    app.nginx_enabled = True
                    await _write_app_nginx_config(app, db, local_node)
            except Exception as e:
                app.status = "error"
                app.last_error = str(e)
        await db.commit()
        imported_count += 1

    return {"message": f"Successfully imported {imported_count} apps."}
