import logging
import time
from typing import Optional
from dbs import engines as db_engines
from dbs.backup_restore import _running_container, _tail
from dbs.docker import _client, _get_container, _progress, _pull, _pull_lock, container_name
from dbs.lifecycle import recreate, states

log = logging.getLogger("cloudbase.databases")


# ── Engine updates ────────────────────────────────────────────────────────────
# Images are tagged per major version (postgres:18, mysql:8.4, …). Docker Hub moves
# such a tag to every new patch release, so an update is: pull the tag again and
# recreate the container on the same volume. Major upgrades need a dump/restore and
# stay a manual step (back up, create the new version, restore).

_VERSION_ENV = ("PG_VERSION", "MYSQL_VERSION", "MARIADB_VERSION", "REDIS_VERSION", "VALKEY_VERSION", "MONGO_VERSION")


UPDATE_HEALTH_TIMEOUT_SECONDS = 600


def _image_version(image) -> Optional[str]:
    import re
    for item in (image.attrs.get("Config") or {}).get("Env") or []:
        key, _, value = item.partition("=")
        if key in _VERSION_ENV:
            m = re.search(r"\d+\.\d+(?:\.\d+)?", value)
            return m.group(0) if m else value
    return None


def check_updates(items: list[dict]) -> dict[str, dict]:
    """For each {database_id, ref}: the running version and whether a newer image
    exists for the tag. Asks the registry once per image."""
    import docker  # type: ignore
    client = _client()
    remote: dict[str, Optional[str]] = {}
    out: dict[str, dict] = {}
    for item in items:
        db_id, ref = int(item["database_id"]), item["ref"]
        info: dict = {}
        try:
            container = _get_container(client, db_id)
            running_image = container.image if container is not None else None
            info["version"] = _image_version(running_image) if running_image is not None else None
            try:
                local = client.images.get(ref)
            except docker.errors.ImageNotFound:
                local = None
            if ref not in remote:
                try:
                    remote[ref] = client.images.get_registry_data(ref).id
                except Exception as e:
                    remote[ref] = None
                    info["error"] = f"Could not reach the registry: {e}"[:300]
            local_digests = {d.split("@", 1)[1] for d in (local.attrs.get("RepoDigests") or [])} if local else set()
            newer_on_hub = bool(remote[ref]) and remote[ref] not in local_digests
            # Pulled already but the container still runs the old image.
            pulled_not_applied = local is not None and running_image is not None and running_image.id != local.id
            info["update_available"] = newer_on_hub or pulled_not_applied
            if pulled_not_applied and local is not None:
                info["latest_version"] = _image_version(local)
        except Exception as e:
            info["error"] = str(e)[:300]
        out[str(db_id)] = info
    return out


def _postgres_refresh_collations(db_id: int) -> list[str]:
    """A new image can come with a newer C library whose text sorting differs
    ("collation version mismatch"); indexes on text must then be rebuilt. Rebuild
    and record the new version for every database that needs it."""
    container = _running_container(db_id)
    find = ('psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "select datname from pg_database '
            'where datallowconn and datcollversion is distinct from pg_database_collation_actual_version(oid)"')
    r = container.exec_run(["sh", "-c", find])
    names = [n.strip() for n in r.output.decode(errors="replace").splitlines()
             if n.strip() and r.exit_code == 0 and not n.startswith(("WARNING", "DETAIL", "HINT"))]
    fixed = []
    for name in names:
        if not name.replace("_", "").replace("-", "").isalnum():
            continue
        cmd = (f"psql -v ON_ERROR_STOP=1 -U \"$POSTGRES_USER\" -d \"{name}\" -c 'REINDEX DATABASE \"{name}\"' "
               f"-c 'ALTER DATABASE \"{name}\" REFRESH COLLATION VERSION'")
        out = container.exec_run(["sh", "-c", cmd])
        if out.exit_code != 0:
            raise RuntimeError(f"Rebuilding indexes of {name} failed: {_tail(out.output)}")
        fixed.append(name)
    if fixed:
        log.info("[db] %s: rebuilt indexes after a collation change in %s", container_name(db_id), ", ".join(fixed))
    return fixed


def update(spec: dict) -> dict:
    """Pull the newest image for the database's tag and recreate the container on the
    same volume. Rolls back to the previous image when the new one does not get healthy."""
    db_id = int(spec["database_id"])
    engine = db_engines.get_engine(spec["engine"])
    ref = engine.image_ref(spec["version"])
    client = _client()
    container = _get_container(client, db_id)
    old_image = container.image if container is not None else None
    old_version = _image_version(old_image) if old_image is not None else None

    with _pull_lock:
        _pull(client, ref, db_id)
    new_image = client.images.get(ref)
    if old_image is not None and new_image.id == old_image.id:
        _progress.pop(db_id, None)
        return {"updated": False, "version": old_version}

    _progress[db_id] = "Starting the new version"
    recreate(spec)
    deadline = time.time() + UPDATE_HEALTH_TIMEOUT_SECONDS
    while time.time() < deadline:
        state = states([db_id])[str(db_id)]["state"]
        if state == "running":
            result = {"updated": True, "from": old_version, "version": _image_version(new_image)}
            if engine.key == "postgres":
                _progress[db_id] = "Rebuilding indexes for the new collation"
                result["reindexed"] = _postgres_refresh_collations(db_id)
            _progress.pop(db_id, None)
            return result
        if state == "restarting":
            break
        time.sleep(3)

    # Not healthy: put the previous image back on the same data.
    if old_image is not None:
        _progress[db_id] = "Update failed, rolling back"
        recreate({**spec, "image_override": old_image.id})
    _progress.pop(db_id, None)
    raise RuntimeError(
        f"The new {engine.label} image did not become healthy within "
        f"{UPDATE_HEALTH_TIMEOUT_SECONDS // 60} minutes; rolled back to {old_version or 'the previous image'}"
    )
