from core import config as _cfg
from containers.client import _apply_linked_databases, _assert_image_local, _get_client, _host_port
from containers.naming import container_name

def _restart_policy_config(policy: str | None) -> dict:
    policy = (policy or "no").strip().lower()
    if policy == "always":
        return {"Name": "always"}
    if policy == "unless-stopped":
        return {"Name": "unless-stopped"}
    if policy == "on-failure":
        return {"Name": "on-failure", "MaximumRetryCount": 5}
    return {"Name": "no"}


# ── Port allocation ───────────────────────────────────────────────────────────

EXTERNAL_PORT_START = _cfg.get_port("instance_min")


EXTERNAL_PORT_END   = _cfg.get_port("instance_max")


def pick_free_external_port(used_ports: set[int]) -> int:
    """Return the lowest unused host port in the configured instance range that is also free on the OS."""
    import psutil
    try:
        active = {c.laddr.port for c in psutil.net_connections(kind="inet") if c.laddr}
    except Exception:
        active = set()

    for p in range(EXTERNAL_PORT_START, EXTERNAL_PORT_END + 1):
        if p not in used_ports and p not in active:
            return p
    raise RuntimeError(
        f"No free external port available in range {EXTERNAL_PORT_START}–{EXTERNAL_PORT_END}"
    )


# ── Run / start ───────────────────────────────────────────────────────────────

def run_container(
    app_id: int,
    app_name: str,
    img: str,
    internal_port: int,
    external_port: int,
    env_vars: dict,
    docker_options: dict | None,
    push_line_fn,
) -> str:
    """Create and start a container. Maps external_port → internal_port. Returns container ID."""
    client = _get_client()
    cname = container_name(app_id)
    docker_options = docker_options or {}

    # Remove any leftover container with the same name
    try:
        old = client.containers.get(cname)
        old.remove(force=True)
        push_line_fn(app_id, "[Docker] Removed old container.")
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
        "restart_policy": _restart_policy_config(docker_options.get("restart_policy")),
        "labels": {
            "cloudbase.app_id": str(app_id),
            "cloudbase.app_name": app_name,
            "cloudbase.internal_port": str(internal_port),
            "cloudbase.external_port": str(external_port),
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
    container = client.containers.run(
        img,
        **run_kwargs,
    )
    push_line_fn(app_id, f"[Docker] Container started: {container.short_id} (:{external_port} → :{internal_port})")
    return container.id


# ── Stop / remove ─────────────────────────────────────────────────────────────

def stop_container(app_id: int, push_line_fn=None) -> bool:
    client = _get_client()
    cname = container_name(app_id)
    try:
        c = client.containers.get(cname)
        c.stop(timeout=10)
        c.remove()
        if push_line_fn:
            push_line_fn(app_id, "[Docker] Container stopped and removed.")
        return True
    except Exception as e:
        if push_line_fn:
            push_line_fn(app_id, f"[Docker] Stop error: {e}")
        return False


# ── Status ────────────────────────────────────────────────────────────────────

def is_container_running(app_id: int) -> bool:
    try:
        client = _get_client()
        c = client.containers.get(container_name(app_id))
        c.reload()
        return c.status == "running"
    except Exception:
        return False
