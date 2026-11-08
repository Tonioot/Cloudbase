import json
from fastapi import APIRouter, BackgroundTasks, HTTPException, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import get_db
from core.models import Application, Node
from containers import dockerfiles as containers_dockerfiles
from cron import scheduler as _cron
from nodes.registry import ensure_local_node
from core.env_crypto import encrypt_env
from core.audit import log_audit
from core import config as _cfg, auth as _auth
from apps import health as health_app
from apps.git import _normalize_repo_url, _encrypt_github_token, _deploy_app, _resolve_token
from apps.nginx_sync import _write_app_nginx_config
from apps.serializers import _app_to_dict
from apps.validation import _check_new_app_name, _validate_docker_runtime_settings, _check_domain_conflicts, _check_repo_url
from routers.applications.schemas import DeployRequest

router = APIRouter(prefix="/api/apps", tags=["applications"])


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
            containers_dockerfiles.resolve_static_dir("", req.start_command or "")
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    # no_web apps (discord bots, background workers, etc.) don't need a port or nginx
    is_no_web = (bool(req.no_web) or is_cron) and not is_static

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
        port=containers_dockerfiles.STATIC_INTERNAL_PORT if is_static else req.port,
        app_type="static" if is_static else None,
        env_vars=encrypt_env(req.env_vars or {}),
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
        health_path=health_app.clean_path(req.health_path) if req.health_path is not None else health_app.DEFAULT_PATH,
        health_expect=health_app.clean_expect(req.health_expect),
        health_timeout_seconds=health_app.clean_timeout(req.health_timeout_seconds or health_app.DEFAULT_TIMEOUT_SECONDS),
    )
    db.add(app)
    await db.commit()
    await db.refresh(app)

    if not target_node.is_local and not is_cron:
        # Nginx runs on the primary and reaches remote instances over the
        # node network, so a custom domain is routed here too. The config itself is
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
