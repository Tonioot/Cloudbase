import json
import threading
from containers.client import _apply_linked_databases, _assert_image_local, _get_client
from containers.dockerfiles import _cmd_to_json
from containers.naming import job_container_name
from containers.replicas import remove_container_by_name

# ── Cron jobs ─────────────────────────────────────────────────────────────────
# A run is one container that does its work and exits; it is removed afterwards.

JOB_OUTPUT_LIMIT = 64 * 1024  # bytes of output kept per run (the end of it)


def image_exists(img: str) -> bool:
    import docker  # type: ignore
    try:
        _get_client().images.get(img)
        return True
    except docker.errors.ImageNotFound:
        return False


def run_job_container(
    app_id: int,
    run_id: int,
    img: str,
    command: str,
    env_vars: dict,
    docker_options: dict | None,
    timeout_seconds: int,
    on_line=None,
) -> dict:
    """Run one cron job to completion. Blocking (call from a thread).

    Returns {"exit_code", "output", "timed_out"}; output is the last
    JOB_OUTPUT_LIMIT bytes. An empty command runs the image's own CMD.
    """
    import time as _time
    client = _get_client()
    docker_options = docker_options or {}
    cname = job_container_name(app_id, run_id)
    remove_container_by_name(cname)

    run_kwargs = {
        "detach": True,
        "name": cname,
        "security_opt": ["no-new-privileges:true"],
        "environment": env_vars,
        "labels": {
            "cloudbase.app_id": str(app_id),
            "cloudbase.cron_run_id": str(run_id),
        },
    }
    if command:
        run_kwargs["command"] = json.loads(_cmd_to_json(command))
    if docker_options.get("cpu_limit"):
        run_kwargs["nano_cpus"] = int(float(docker_options["cpu_limit"]) * 1_000_000_000)
    if docker_options.get("memory_limit_mb"):
        run_kwargs["mem_limit"] = int(docker_options["memory_limit_mb"]) * 1024 * 1024
    if docker_options.get("read_only_root"):
        run_kwargs["read_only"] = True
    if docker_options.get("tmpfs_enabled"):
        tmpfs_opts = ["rw", "nosuid", "nodev", "noexec"]
        if docker_options.get("tmpfs_size_mb"):
            tmpfs_opts.append(f"size={int(docker_options['tmpfs_size_mb'])}m")
        run_kwargs["tmpfs"] = {"/tmp": ",".join(tmpfs_opts)}
    _apply_linked_databases(run_kwargs, docker_options)
    _assert_image_local(client, img)

    container = client.containers.run(img, **run_kwargs)
    output = bytearray()
    timed_out = False
    deadline = _time.monotonic() + max(1, int(timeout_seconds or 3600))

    def _follow():
        pending = b""
        try:
            for chunk in container.logs(stream=True, follow=True):
                output.extend(chunk)
                if len(output) > JOB_OUTPUT_LIMIT * 2:
                    del output[:len(output) - JOB_OUTPUT_LIMIT]
                if on_line:
                    pending += chunk
                    *lines, pending = pending.split(b"\n")
                    for line in lines:
                        on_line(line.decode("utf-8", "replace").rstrip("\r"))
        except Exception:
            pass
        if on_line and pending:
            on_line(pending.decode("utf-8", "replace"))

    follower = threading.Thread(target=_follow, daemon=True)
    follower.start()
    try:
        exit_code = None
        while exit_code is None:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                timed_out = True
                try:
                    container.stop(timeout=10)
                except Exception:
                    pass
                break
            try:
                exit_code = container.wait(timeout=min(remaining, 30)).get("StatusCode")
            except Exception:
                container.reload()
                if container.status in ("exited", "dead"):
                    exit_code = container.attrs.get("State", {}).get("ExitCode")
        if exit_code is None:
            try:
                container.reload()
                exit_code = container.attrs.get("State", {}).get("ExitCode")
            except Exception:
                pass
        follower.join(timeout=5)
    finally:
        try:
            container.remove(force=True)
        except Exception:
            pass
    text = bytes(output[-JOB_OUTPUT_LIMIT:]).decode("utf-8", "replace")
    return {"exit_code": exit_code, "output": text, "timed_out": timed_out}


def list_job_containers() -> list[str]:
    """Names of cron job containers on this machine (left over after a restart)."""
    client = _get_client()
    return [c.name for c in client.containers.list(all=True) if c.name.startswith("cloudbase-cron-")]
