"""Turn an export (see manifest.py) into applications on this installation.

Two phases, so the request can answer in a moment:
  import_apps()      database writes only: validates every app the way creating one does, then
                     creates the app and its instances. Apps that cannot be imported are skipped
                     with a reason; nothing else is affected.
  deploy_imported()  runs afterwards in the background: clones and builds each app and starts the
                     ones that were running when they were exported.
"""
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps import health
from apps.git import _deploy_app, _encrypt_github_token, _normalize_repo_url
from apps.lifecycle import start_app_now
from apps.nginx_sync import _write_app_nginx_config
from apps.validation import (
    _assign_external_port,
    _check_domain_conflicts,
    _check_git_ref,
    _check_new_app_name,
    _check_repo_url,
    _validate_docker_runtime_settings,
)
from containers import dockerfiles as containers_dockerfiles
from core import config as _cfg
from core.audit import log_audit
from core.database import AsyncSessionLocal
from core.env_crypto import encrypt_env
from core.models import Application, ApplicationReplica, Node
from cron import scheduler as _cron
from nodes.registry import ensure_local_node

log = logging.getLogger("cloudbase.transfer")

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_INSTANCES_PER_APP = 50


@dataclass
class Imported:
    id: int
    name: str
    instances: int
    run: bool


@dataclass
class ImportResult:
    imported: list[Imported] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    target_is_local: bool = True


def _clean_env(raw, name: str, warnings: list[str]) -> dict[str, str]:
    env = {}
    for key, value in (raw or {}).items() if isinstance(raw, dict) else []:
        if isinstance(key, str) and _ENV_NAME.match(key) and isinstance(value, (str, int, float, bool)):
            env[key] = str(value)
        else:
            warnings.append(f"{name}: environment variable {key!r} was left out (invalid name or value)")
    return env


def _health(clean, value):
    """A health check setting from a file: valid ones are kept, anything else falls back to the default."""
    if value is None:
        return None
    try:
        return clean(value)
    except HTTPException:
        return None


def _opt(entry: dict, key: str, kind=None):
    """Value from the file, or None when absent (so the column default applies)."""
    value = entry.get(key)
    if value is None:
        return None
    try:
        return kind(value) if kind else value
    except (TypeError, ValueError):
        return None


async def import_apps(db: AsyncSession, entries: list[dict], target_node_id: Optional[int], actor: str, perms=None) -> ImportResult:
    """perms: the importing user's permissions, or None for the root account (which may do everything)."""
    local_node = await ensure_local_node(db)
    target = local_node
    if target_node_id is not None:
        target = (await db.execute(select(Node).where(Node.id == target_node_id, Node.enabled == True))).scalar_one_or_none()  # noqa: E712
        if not target:
            raise HTTPException(400, "The selected node is not available")

    # Plain values, not the Node object: a rollback after a skipped app expires every loaded object,
    # and touching an expired one in async code raises.
    target_id, target_is_local = target.id, bool(target.is_local)
    result = ImportResult(target_is_local=target_is_local)
    for entry in entries:
        name = str(entry.get("name") or "").strip()
        try:
            if perms is not None and "apps.deploy" not in perms:
                raise HTTPException(403, "you need the 'apps.deploy' permission to import apps")
            result.imported.append(await _import_one(db, entry, target_id, target_is_local, actor, result.warnings))
        except HTTPException as exc:
            await db.rollback()
            result.skipped.append({"name": name or "(no name)", "reason": str(exc.detail)})
        except Exception as exc:  # one broken entry must not stop the others
            await db.rollback()
            log.exception("[transfer] import of %r failed", name)
            result.skipped.append({"name": name or "(no name)", "reason": f"unexpected error: {exc}"})
    return result


async def _import_one(db: AsyncSession, entry: dict, target_id: int, target_is_local: bool, actor: str, warnings: list[str]) -> Imported:
    name = await _check_new_app_name(entry.get("name"), db)
    repo_url = _check_repo_url(_normalize_repo_url(entry.get("repo_url") or ""))
    domains = [d for d in [entry.get("domain"), *(entry.get("extra_domains") or []), *(entry.get("redirect_domains") or [])] if d]
    await _check_domain_conflicts(domains, db)

    if (await db.execute(select(func.count()).select_from(Application))).scalar() >= _cfg.get_limit("max_apps"):
        raise HTTPException(400, f"App limit reached ({_cfg.get_limit('max_apps')} apps maximum)")
    if entry.get("restart_policy") not in (None, "no", "always", "on-failure"):
        raise HTTPException(400, "restart_policy must be one of: no, always, on-failure")
    _validate_docker_runtime_settings(entry.get("docker_cpu_limit"), entry.get("docker_memory_limit_mb"), entry.get("docker_tmpfs_size_mb"))
    branch = _check_git_ref(entry.get("deploy_branch"))
    strategy = entry.get("deploy_strategy")
    if strategy not in (None, "rolling", "blue_green"):
        strategy = None

    is_static = (entry.get("app_type") or "").strip().lower() == "static"
    cron_schedule = " ".join((entry.get("cron_schedule") or "").split()) or None
    if cron_schedule and is_static:
        cron_schedule = None
    if cron_schedule:
        try:
            _cron.check_schedule(cron_schedule, entry.get("cron_timezone"))
        except HTTPException as exc:
            warnings.append(f"{name}: schedule dropped, it is imported as a worker ({exc.detail})")
            cron_schedule = None
    is_cron = bool(cron_schedule)
    no_web = (bool(entry.get("no_web")) or is_cron) and not is_static
    env = _clean_env(entry.get("env_vars"), name, warnings)
    if entry.get("env_names") and not env:
        warnings.append(f"{name}: this export has no secrets. Set these environment variables yourself: {', '.join(map(str, entry['env_names']))}")
    if entry.get("has_git_token") and not entry.get("git_token"):
        warnings.append(f"{name}: add its Git token in the app's settings")

    # Optional settings: only passed when the file has them, so a missing one keeps its default.
    optional = {k: v for k, v in {
        "auto_deploy": _opt(entry, "auto_deploy", bool),
        "deploy_branch": branch,
        "deploy_strategy": strategy,
        "auto_deploy_interval": (max(30, min(_opt(entry, "auto_deploy_interval", int), 3600))
                                 if _opt(entry, "auto_deploy_interval", int) is not None else None),
        "autoscale_enabled": _opt(entry, "autoscale_enabled", bool),
        "autoscale_min_replicas": _opt(entry, "autoscale_min_replicas", int),
        "autoscale_max_replicas": _opt(entry, "autoscale_max_replicas", int),
        "autoscale_cpu_target": _opt(entry, "autoscale_cpu_target", int),
        "health_path": _health(health.clean_path, entry.get("health_path")),
        "health_expect": _health(health.clean_expect, entry.get("health_expect")),
        "health_timeout_seconds": _health(health.clean_timeout, entry.get("health_timeout_seconds")),
    }.items() if v is not None}

    app = Application(
        name=name,
        repo_url=repo_url,
        github_token=_encrypt_github_token(entry.get("git_token") or None),
        domain=entry.get("domain") or None,
        extra_domains=json.dumps(entry.get("extra_domains") or []),
        redirect_domains=json.dumps(entry.get("redirect_domains") or []),
        start_command=entry.get("start_command"),
        build_command=(entry.get("build_command") or "").strip() or None,
        port=containers_dockerfiles.STATIC_INTERNAL_PORT if is_static else _opt(entry, "port", int),
        app_type="static" if is_static else None,
        env_vars=encrypt_env(env),
        restart_policy=entry.get("restart_policy") or "no",
        no_web=no_web,
        docker_cpu_limit=entry.get("docker_cpu_limit"),
        docker_memory_limit_mb=entry.get("docker_memory_limit_mb"),
        docker_read_only_root=bool(entry.get("docker_read_only_root")),
        docker_tmpfs_enabled=bool(entry.get("docker_tmpfs_enabled")),
        docker_tmpfs_size_mb=entry.get("docker_tmpfs_size_mb"),
        source_revision=entry.get("source_revision"),
        image_revision=entry.get("image_revision"),
        status="deploying",
        node_id=target_id if not target_is_local else None,
        maintenance_mode=bool(entry.get("maintenance_mode")),
        update_mode=bool(entry.get("update_mode")),
        downtime_page=json.dumps(entry.get("downtime_page") or {}),
        update_page=json.dumps(entry.get("update_page") or {}),
        restart_page=json.dumps(entry.get("restart_page") or {}),
        starting_page=json.dumps(entry.get("starting_page") or {}),
        cron_schedule=cron_schedule,
        cron_timezone=(entry.get("cron_timezone") or None) if is_cron else None,
        cron_enabled=bool(entry.get("cron_enabled", True)),
        cron_timeout_seconds=entry.get("cron_timeout_seconds") if entry.get("cron_timeout_seconds") in _cron.TIMEOUT_CHOICES else 3600,
        **optional,
    )
    db.add(app)
    await db.flush()   # gets the id; app and instances are committed together below

    instances = 0
    wanted = 0 if is_cron else max(0, min(int(entry.get("instances") or 0), MAX_INSTANCES_PER_APP))
    existing = (await db.execute(select(func.count()).select_from(ApplicationReplica))).scalar() or 0
    room = max(0, _cfg.get_limit("max_instances") - existing)
    if wanted > room:
        warnings.append(f"{name}: only {room} of {wanted} instances created (instance limit reached)")
        wanted = room
    for _ in range(wanted):
        db.add(ApplicationReplica(app_id=app.id, node_id=target_id, status="stopped",
                                  external_port=await _assign_external_port(None, target_id, db)))
        await db.flush()
        instances += 1
    await log_audit(db, "app.import", actor=actor, app_id=app.id, detail={"name": app.name, "instances": instances})
    await db.commit()
    return Imported(id=app.id, name=app.name, instances=instances, run=bool(entry.get("run")) and instances > 0)


async def deploy_imported(imported: list[Imported], target_is_local: bool, actor: str) -> None:
    """Clone and build each imported app (one at a time) and start the ones that were running."""
    for item in imported:
        try:
            async with AsyncSessionLocal() as db:
                app = (await db.execute(select(Application).where(Application.id == item.id))).scalar_one_or_none()
                if not app:
                    continue
                local_node = await ensure_local_node(db)
                is_cron = bool(app.cron_schedule)
                if target_is_local or is_cron:   # a remote node gets its source when the instance starts there
                    await _deploy_app(app)
                    app.status = _cron.status_of(app) if is_cron else "stopped"
                else:
                    app.status = "stopped" if item.instances else "deploying"
                if app.domain and not app.no_web:
                    app.nginx_enabled = True
                    if target_is_local:
                        await _write_app_nginx_config(app, db, local_node)
                await db.commit()
                if item.run and not is_cron:
                    await start_app_now(app.id, db, actor)
        except Exception as exc:
            log.exception("[transfer] deploying imported app %r failed", item.name)
            try:
                async with AsyncSessionLocal() as db:
                    app = (await db.execute(select(Application).where(Application.id == item.id))).scalar_one_or_none()
                    if app:
                        app.status = "error"
                        app.last_error = str(getattr(exc, "detail", exc))[:500]
                        await db.commit()
            except Exception:
                log.exception("[transfer] could not record the error for %r", item.name)
