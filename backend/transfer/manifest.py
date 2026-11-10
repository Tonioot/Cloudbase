"""The manifest of an export file: a plain dict, written as JSON into the archive (archive.py).

Everything that is specific to one installation is left out or translated:
ids, ports, the node an app runs on, file paths on this server and the encryption key.
Secrets (environment variables, the Git token) are written in clear text, because the key that
protects them here does not exist on the other server. That is why they are only included on
request, with the secrets.export permission, and why the file must be treated like a password.
"""
import json
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.env_crypto import decrypt_env, decrypt_text
from core.models import Application, Instance

FORMAT = "cloudbase-export"
VERSION = 1

# Copied one to one from the Application row.
PLAIN_FIELDS = (
    "repo_url", "domain", "app_type", "start_command", "build_command", "port", "no_web",
    "restart_policy", "auto_deploy", "deploy_branch", "deploy_strategy", "auto_deploy_interval",
    "docker_cpu_limit", "docker_memory_limit_mb", "docker_read_only_root", "docker_tmpfs_enabled", "docker_tmpfs_size_mb",
    "autoscale_enabled", "autoscale_min_instances", "autoscale_max_instances", "autoscale_cpu_target",
    "autoscale_memory_target_mb", "autoscale_up_seconds", "autoscale_down_minutes", "autoscale_max_step_up",
    "autoscale_max_step_down",
    "maintenance_mode", "update_mode", "source_revision", "image_revision",
    "cron_schedule", "cron_timezone", "cron_enabled", "cron_timeout_seconds",
    "health_path", "health_expect", "health_timeout_seconds",
)
# Stored as JSON text in the row, written as real JSON in the file.
JSON_FIELDS = ("extra_domains", "redirect_domains", "downtime_page", "update_page", "restart_page", "starting_page")
JSON_DEFAULTS = {"extra_domains": [], "redirect_domains": [], "downtime_page": {}, "update_page": {}, "restart_page": {}, "starting_page": {}}


def _json(value: Optional[str], default):
    try:
        return json.loads(value) if value else default
    except ValueError:
        return default


def app_entry(app: Application, instances: list[Instance], include_secrets: bool) -> dict:
    entry = {"name": app.name}
    for field in PLAIN_FIELDS:
        entry[field] = getattr(app, field)
    for field in JSON_FIELDS:
        entry[field] = _json(getattr(app, field), JSON_DEFAULTS[field])
    env = decrypt_env(app.env_vars or "")
    if include_secrets:
        entry["env_vars"] = env
        entry["git_token"] = (decrypt_text(app.github_token) or None) if app.github_token else None
    else:
        # Names only, so the import can tell you what to fill in. Never the values.
        entry["env_vars"] = {}
        entry["env_names"] = sorted(env)
        entry["git_token"] = None
        entry["has_git_token"] = bool(app.github_token)
    entry["instances"] = len(instances)
    # Should the import start it? Yes when it was running (cron jobs: when enabled, which is a field above).
    entry["run"] = any(r.status in ("running", "starting") for r in instances)
    return entry


async def build_manifest(db: AsyncSession, app_ids: Optional[list[int]] = None, include_secrets: bool = False) -> dict:
    query = select(Application).order_by(Application.name)
    if app_ids is not None:               # None: every app. A list, even an empty one: exactly those
        query = query.where(Application.id.in_(app_ids))
    apps = (await db.execute(query)).scalars().all()

    instances_by_app: dict[int, list[Instance]] = {a.id: [] for a in apps}
    if apps:
        rows = await db.execute(select(Instance).where(Instance.app_id.in_(instances_by_app)))
        for r in rows.scalars():
            instances_by_app[r.app_id].append(r)

    return {
        "format": FORMAT,
        "version": VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "secrets_included": include_secrets,
        "apps": [app_entry(a, instances_by_app[a.id], include_secrets) for a in apps],
    }


def check_manifest(manifest) -> list[dict]:
    """Return the app entries, or raise ValueError saying why this is not a usable export."""
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise ValueError("This is not a Cloudbase export file")
    if int(manifest.get("version") or 0) > VERSION:
        raise ValueError("This export was made by a newer Cloudbase. Update this server first, then import it")
    apps = manifest.get("apps")
    if not isinstance(apps, list) or not all(isinstance(a, dict) for a in apps):
        raise ValueError("The export file is missing its list of apps")
    return apps


def databases_of(manifest: dict) -> list[dict]:
    """The database entries of an export (older files have none)."""
    entries = manifest.get("databases") or []
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise ValueError("The export file has a damaged list of databases")
    return entries

