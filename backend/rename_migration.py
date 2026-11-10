"""TEMPORARY: the rename of "replica" to "instance" (November 2026). Remove once every server runs this version.

To remove: delete this file and tests/test_rename_migration.py, and the lines that call it
(`git grep rename_migration`). Nothing else depends on it.

What it does, once per server:
* database (primary): table application_replicas -> application_instances, applications.autoscale_*_replicas ->
  autoscale_*_instances, and "replica_id" -> "instance_id" inside the stored specs (their hash stays, so no node
  recreates a container for it)
* Docker (every node, the primary too): containers cloudbase-app-<a>-replica-<i> are renamed to
  cloudbase-app-<a>-instance-<i> while they keep running
* import: export files made before the rename use the old autoscale names
"""
import json
import logging
import re

from core.env_crypto import decrypt_text, encrypt_text

log = logging.getLogger("cloudbase.main")

_OLD_TABLE, _NEW_TABLE = "application_replicas", "application_instances"
_OLD_KEY = "replica_id"
_COLUMNS = {"autoscale_min_replicas": "autoscale_min_instances", "autoscale_max_replicas": "autoscale_max_instances"}
_OLD_CONTAINER = re.compile(r"^cloudbase-app-(\d+)-replica-(\d+)$")


async def migrate_database(conn) -> None:
    """Run before create_all, so it does not create the new, empty table next to the old one."""
    tables = {row[0] for row in (await conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'table'")).fetchall()}
    if _OLD_TABLE in tables and _NEW_TABLE not in tables:
        await conn.exec_driver_sql(f"ALTER TABLE {_OLD_TABLE} RENAME TO {_NEW_TABLE}")
        for index in ("id", "app_id", "node_id"):                      # create_all makes them again under the new name
            await conn.exec_driver_sql(f"DROP INDEX IF EXISTS ix_{_OLD_TABLE}_{index}")
        rows = (await conn.exec_driver_sql(f"SELECT id, spec_json FROM {_NEW_TABLE} WHERE spec_json IS NOT NULL")).fetchall()
        for row_id, spec_json in rows:
            spec = json.loads(decrypt_text(spec_json))
            if _OLD_KEY in spec:
                spec["instance_id"] = spec.pop(_OLD_KEY)
                await conn.exec_driver_sql(f"UPDATE {_NEW_TABLE} SET spec_json = ? WHERE id = ?",
                                           (encrypt_text(json.dumps(spec)), row_id))
        log.info("[rename] table %s is now %s (%d stored specs)", _OLD_TABLE, _NEW_TABLE, len(rows))
    if "applications" in tables:
        columns = {row[1] for row in (await conn.exec_driver_sql("PRAGMA table_info(applications)")).fetchall()}
        for old, new in _COLUMNS.items():
            if old in columns and new not in columns:
                await conn.exec_driver_sql(f"ALTER TABLE applications RENAME COLUMN {old} TO {new}")


_containers_done = False


def rename_old_containers(client) -> None:
    """Give running containers their new name (Docker renames a running container in place). Once per process."""
    global _containers_done
    if _containers_done:
        return
    for container in client.containers.list(all=True):
        m = _OLD_CONTAINER.match(container.name)
        if m:
            new = f"cloudbase-app-{m.group(1)}-instance-{m.group(2)}"
            container.rename(new)
            log.info("[rename] container %s is now %s", container.name, new)
    _containers_done = True


def old_export_keys(entry: dict) -> dict:
    """An export file from before the rename: its autoscale settings under their new names."""
    return {**{_COLUMNS[k]: v for k, v in entry.items() if k in _COLUMNS}, **{k: v for k, v in entry.items() if k not in _COLUMNS}}
