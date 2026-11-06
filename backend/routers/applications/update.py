import asyncio
import json
import os
import re
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import get_db
from containers import processes as pm, dockerfiles as containers_dockerfiles
from nginx import certificates as nginx_certificates, config as nginx_config, files as nginx_files
from nodes.registry import ensure_local_node
from core.env_crypto import decrypt_env, encrypt_env
from core.audit import log_audit
from core import auth as _auth, system_config as _syscfg
from apps.git import _encrypt_github_token, _resolve_token
from apps.images import _image_port, _dockerfile_build_kwargs
from apps.nginx_sync import _resolve_ssl_paths, _write_app_nginx_config, _get_nginx_backends, _get_nginx_mode, _best_effort_remove_app_nginx, _ensure_maintenance_files
from apps.serializers import _app_to_dict, _get_or_404
from apps.validation import _validate_docker_runtime_settings, _check_domain_conflicts, _assign_external_port
from routers.applications.schemas import UpdateRequest

router = APIRouter(prefix="/api/apps", tags=["applications"])


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
        await nginx_certificates.forget_pending([d for d in all_domains if d])

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
                containers_dockerfiles.resolve_static_dir("", req.start_command)
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
                containers_dockerfiles.ensure_dockerfile,
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
        config = nginx_config.generate_config(
            app.name, app.domain, backends,
            ssl_cert, ssl_key,
            app_id=app.id, mode=_get_nginx_mode(app),
            extra_domains=json.loads(app.extra_domains or "[]"),
            redirect_domains=json.loads(app.redirect_domains or "[]"),
        )
        ok, msg = nginx_files.write_nginx_config(app.name, config)
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
