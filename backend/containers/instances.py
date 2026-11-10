import logging

import rename_migration
from containers.apps import _restart_policy_config
from containers.client import _apply_linked_databases, _assert_image_local, _get_client, _host_port
from containers.naming import instance_container_name

log = logging.getLogger("cloudbase.docker")


def _instance_restart_policy_config(docker_options: dict) -> dict:
    """Instance containers always use 'unless-stopped' so Docker auto-restarts them
    after a node reboot. Explicit 'always' or 'on-failure' from docker_options is honoured."""
    policy = (docker_options.get("restart_policy") or "").strip().lower()
    if policy in ("always", "on-failure"):
        return _restart_policy_config(policy)
    return {"Name": "unless-stopped"}


def run_instance_container(
    app_id: int,
    instance_id: int,
    img: str,
    internal_port: int,
    external_port: int,
    env_vars: dict,
    docker_options: dict | None,
    push_line_fn,
) -> str:
    """Start a instance container. Returns container ID."""
    client = _get_client()
    cname = instance_container_name(app_id, instance_id)
    docker_options = docker_options or {}
    import time as _time
    import docker as _docker_mod  # type: ignore

    try:
        old = client.containers.get(cname)
        old.reload()
        if old.status == "running":
            # Container survived a node outage and is still healthy — reuse it
            push_line_fn(app_id, f"[Instance] Container {instance_id} already running, reusing (recovered from outage).")
            return old.id
        old.remove(force=True)
        push_line_fn(app_id, f"[Instance] Removed old container for instance {instance_id}.")
        # Wait for Docker to fully release the container name and port bindings
        for _ in range(10):
            try:
                client.containers.get(cname)
                _time.sleep(0.5)
            except Exception:
                break
    except Exception:
        pass

    port_bindings = {}
    if internal_port and external_port and docker_options.get("publish_port", True):
        port_bindings[f"{internal_port}/tcp"] = _host_port(external_port, docker_options)

    run_kwargs = {
        "detach": True,
        "name": cname,
        "ports": port_bindings,
        # setuid binaries (sudo, su) inside the image cannot raise privileges
        "security_opt": ["no-new-privileges:true"],
        "environment": env_vars,
        "restart_policy": _instance_restart_policy_config(docker_options),
        "labels": {
            "cloudbase.app_id": str(app_id),
            "cloudbase.instance_id": str(instance_id),
            "cloudbase.internal_port": str(internal_port),
            "cloudbase.external_port": str(external_port),
            # which start of this instance it is (nodes compare it with what the primary wants)
            "cloudbase.spec": str(docker_options.get("spec_hash") or ""),
        },
    }

    cpu_limit = docker_options.get("cpu_limit")
    if cpu_limit:
        run_kwargs["nano_cpus"] = int(float(cpu_limit) * 1_000_000_000)

    memory_limit_mb = docker_options.get("memory_limit_mb")
    if memory_limit_mb:
        run_kwargs["mem_limit"] = int(memory_limit_mb) * 1024 * 1024

    if docker_options.get("read_only_root"):
        run_kwargs["read_only"] = True

    if docker_options.get("tmpfs_enabled"):
        tmpfs_opts = ["rw", "nosuid", "nodev", "noexec"]
        tmpfs_size_mb = docker_options.get("tmpfs_size_mb")
        if tmpfs_size_mb:
            tmpfs_opts.append(f"size={int(tmpfs_size_mb)}m")
        run_kwargs["tmpfs"] = {"/tmp": ",".join(tmpfs_opts)}

    _apply_linked_databases(run_kwargs, docker_options)
    _assert_image_local(client, img)
    # Retry on transient Docker errors: port still releasing or container name
    # still being removed after a force-remove.
    for attempt in (1, 2, 3):
        try:
            container = client.containers.run(img, **run_kwargs)
            break
        except _docker_mod.errors.APIError as exc:
            err = str(exc)
            if attempt == 3:
                raise
            if "port is already allocated" in err:
                push_line_fn(app_id, f"[Instance] Port {external_port} still releasing, retrying in 2 s…")
                _time.sleep(2)
            elif "already in use by container" in err:
                # Previous force-remove hasn't completed yet — wait and retry
                push_line_fn(app_id, "[Instance] Container name still releasing, retrying in 2 s…")
                try:
                    client.containers.get(cname).remove(force=True)
                except Exception:
                    pass
                _time.sleep(2)
            else:
                raise
    ports = f" (:{external_port} → :{internal_port})" if port_bindings else " (no port: background worker)"
    push_line_fn(app_id, f"[Instance] Container {instance_id} started: {container.short_id}{ports}")
    return container.id


def stop_instance_container(app_id: int, instance_id: int, push_line_fn=None) -> bool:
    import docker  # type: ignore
    client = _get_client()
    cname = instance_container_name(app_id, instance_id)
    try:
        c = client.containers.get(cname)
        c.stop(timeout=10)
        c.remove()
        if push_line_fn:
            push_line_fn(app_id, f"[Instance] Container {instance_id} stopped and removed.")
        return True
    except docker.errors.NotFound:
        # Container already gone — treat as success.
        return True
    except Exception as e:
        if push_line_fn:
            push_line_fn(app_id, f"[Instance] Stop error for instance {instance_id}: {e}")
        return False


# ── Orphan cleanup ─────────────────────────────────────────────────────────────

def list_instance_containers() -> list[dict]:
    """Return all local instance containers (running or not) with their parsed
    app_id / instance_id. Used by the orphan cleaner to reconcile against the DB."""
    import re
    client = _get_client()
    rename_migration.rename_old_containers(client)          # TEMPORARY, see rename_migration.py
    pattern = re.compile(r"^cloudbase-app-(\d+)-instance-(\d+)$")
    out: list[dict] = []
    for c in client.containers.list(all=True):
        m = pattern.match(c.name)
        if not m:
            continue
        out.append({
            "name": c.name,
            "id": c.id,
            "app_id": int(m.group(1)),
            "instance_id": int(m.group(2)),
            "status": c.status,
            "spec": (c.labels or {}).get("cloudbase.spec") or None,
            "exit_code": (c.attrs.get("State") or {}).get("ExitCode"),
        })
    return out


def remove_container_by_name(cname: str) -> bool:
    """Force-stop and remove a container by name. Returns True if it's gone."""
    import docker  # type: ignore
    client = _get_client()
    try:
        c = client.containers.get(cname)
        c.remove(force=True)
        log.info("[docker] Removed orphan container %s", cname)
        return True
    except docker.errors.NotFound:
        return True
    except Exception:
        log.warning("[docker] Failed to remove container %s", cname, exc_info=True)
        return False
