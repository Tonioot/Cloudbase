"""The list of instances a node asks for (see desired.py for what the list means)."""
import asyncio
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.git import _refresh_app_source_revision
from apps.runtime import _remote_replica_command_payload
from core.env_crypto import decrypt_env, decrypt_text, encrypt_text
from core.models import Application, ApplicationReplica
from nodes.desired import list_hash, spec_hash


async def _write_missing_specs(db: AsyncSession, replicas: list[ApplicationReplica]) -> None:
    """Instances from before desired state have no spec yet: write one from the app's current settings.

    The node adopts the container it already runs for such an instance instead of recreating it."""
    revisions: dict[int, str | None] = {}
    for replica in replicas:
        if replica.desired != "running" or replica.spec_json:
            continue
        app = await db.get(Application, replica.app_id)
        if app is None:
            continue
        if app.id not in revisions:
            revisions[app.id] = await asyncio.to_thread(_refresh_app_source_revision, app)
        payload = _remote_replica_command_payload(
            app, decrypt_env(app.env_vars or ""), replica.external_port or app.port or 8000, revisions[app.id])
        payload.update({"docker_cpu_limit": replica.docker_cpu_limit, "docker_memory_limit_mb": replica.docker_memory_limit_mb,
                        "docker_read_only_root": replica.docker_read_only_root, "docker_tmpfs_enabled": replica.docker_tmpfs_enabled,
                        "docker_tmpfs_size_mb": replica.docker_tmpfs_size_mb})
        spec = {**payload, "replica_id": replica.id}
        replica.spec_json, replica.spec_hash = encrypt_text(json.dumps(spec)), spec_hash(spec)
    await db.commit()


async def snapshot(db: AsyncSession, node_id: int) -> dict:
    """The list a node asks for: every instance placed on it, with what it should do."""
    replicas = (await db.execute(
        select(ApplicationReplica).where(ApplicationReplica.node_id == node_id).order_by(ApplicationReplica.id)
    )).scalars().all()
    await _write_missing_specs(db, replicas)
    entries = []
    for replica in replicas:
        wanted = replica.desired or "stopped"
        spec = json.loads(decrypt_text(replica.spec_json)) if wanted == "running" and replica.spec_json else None
        if wanted == "running" and spec is None:
            wanted = "stopped"                     # its app is gone; there is nothing to run
        entries.append({"replica_id": replica.id, "app_id": replica.app_id, "desired": wanted,
                        "spec_hash": replica.spec_hash if spec else None, "spec": spec})
    return {"hash": list_hash(entries), "replicas": entries}
