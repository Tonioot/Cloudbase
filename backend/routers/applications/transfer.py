import json
import logging
from fastapi import APIRouter, BackgroundTasks, HTTPException, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import get_db
from core.models import Application, Node
from cron import scheduler as _cron
from nodes.registry import ensure_local_node
from core.env_crypto import encrypt_env
from core import auth as _auth
from apps.git import _encrypt_github_token, _deploy_app
from apps.nginx_sync import _write_app_nginx_config
from apps.validation import _check_new_app_name, _assign_external_port, _check_repo_url
from routers.applications.schemas import ImportRequest, ExportRequest

router = APIRouter(prefix="/api/apps", tags=["applications"])


log = logging.getLogger("cloudbase.apps")


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
