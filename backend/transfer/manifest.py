"""The content of an export file: a plain dict, written as JSON and then encrypted (crypto.py).

Everything that is specific to one installation is left out or translated:
ids, ports, the node an app runs on, file paths on this server and the encryption key.
Secrets (environment variables, the Git token) are written in clear text *inside* the encrypted
file, because the key that protects them here does not exist on the other server.
"""
import json
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.env_crypto import decrypt_env, decrypt_text
from core.models import Application, ApplicationReplica

FORMAT = "cloudbase-export"
VERSION = 1

# Copied one to one from the Application row.
PLAIN_FIELDS = (
    "repo_url", "domain", "app_type", "start_command", "build_command", "port", "no_web",
    "auto_start", "restart_policy", "auto_deploy", "deploy_branch", "deploy_strategy", "auto_deploy_interval",
    "docker_cpu_limit", "docker_memory_limit_mb", "docker_read_only_root", "docker_tmpfs_enabled", "docker_tmpfs_size_mb",
    "autoscale_enabled", "autoscale_min_replicas", "autoscale_max_replicas", "autoscale_cpu_target",
    "maintenance_mode", "update_mode", "source_revision", "image_revision",
    "cron_schedule", "cron_timezone", "cron_enabled", "cron_timeout_seconds",
)
# Stored as JSON text in the row, written as real JSON in the file.
JSON_FIELDS = ("extra_domains", "redirect_domains", "downtime_page", "update_page", "restart_page", "starting_page")
JSON_DEFAULTS = {"extra_domains": [], "redirect_domains": [], "downtime_page": {}, "update_page": {}, "restart_page": {}, "starting_page": {}}


def _json(value: Optional[str], default):
    try:
        return json.loads(value) if value else default
    except ValueError:
        return default


def app_entry(app: Application, replicas: list[ApplicationReplica]) -> dict:
    entry = {"name": app.name}
    for field in PLAIN_FIELDS:
        entry[field] = getattr(app, field)
    for field in JSON_FIELDS:
        entry[field] = _json(getattr(app, field), JSON_DEFAULTS[field])
    entry["env_vars"] = decrypt_env(app.env_vars or "")
    entry["git_token"] = (decrypt_text(app.github_token, fallback_plaintext=True) or None) if app.github_token else None
    entry["instances"] = len(replicas)
    # Should the import start it? Yes when it was running (cron jobs: when enabled, which is a field above).
    entry["run"] = any(r.status in ("running", "starting") for r in replicas)
    return entry


async def build_manifest(db: AsyncSession, app_ids: Optional[list[int]] = None) -> dict:
    query = select(Application).order_by(Application.name)
    if app_ids:
        query = query.where(Application.id.in_(app_ids))
    apps = (await db.execute(query)).scalars().all()

    replicas_by_app: dict[int, list[ApplicationReplica]] = {a.id: [] for a in apps}
    if apps:
        rows = await db.execute(select(ApplicationReplica).where(ApplicationReplica.app_id.in_(replicas_by_app)))
        for r in rows.scalars():
            replicas_by_app[r.app_id].append(r)

    return {
        "format": FORMAT,
        "version": VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "apps": [app_entry(a, replicas_by_app[a.id]) for a in apps],
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


async def count_apps(db: AsyncSession) -> int:
    return (await db.execute(select(func.count()).select_from(Application))).scalar() or 0
