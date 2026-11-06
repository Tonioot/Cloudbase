import os
import asyncio
import subprocess
from containers import client as containers_client, naming as containers_naming, replicas as containers_replicas, cleanup as containers_cleanup, jobs as containers_jobs
from agent.log import AGENT_LOG_FILE, _agent_log
from agent.source import _build_image_local, _download_and_extract_source, _ensure_replica_app_deployed, _report_replica_substatus, _wait_local_api
from agent.urls import _LOCAL_API_BASE

# ─── Command Handlers ─────────────────────────────────────────────────────────


async def cmd_delete_app(_client, _state, main_id, _payload, _headers):
    """Stop all replica containers for this app on this node."""
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}", "--filter", f"name=cloudbase-app-{main_id}-replica-"],
            capture_output=True, text=True, timeout=10,
        )
        for cname in result.stdout.splitlines():
            cname = cname.strip()
            if cname:
                subprocess.run(["docker", "rm", "-f", cname], capture_output=True, timeout=15)
                _agent_log(f"[delete_app] Removed container '{cname}'")
    except Exception as e:
        _agent_log(f"[delete_app] Error stopping containers for app {main_id}: {e}")
    return {"message": f"Containers for app {main_id} removed"}


async def cmd_get_logs_tail(_client, _state, main_id, payload, _headers):
    """Get recent logs from all replica containers for this app."""
    limit = payload.get("limit") or 200
    lines: list[str] = []
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}", "--filter", f"name=cloudbase-app-{main_id}-replica-"],
            capture_output=True, text=True, timeout=10,
        )
        for cname in result.stdout.splitlines():
            cname = cname.strip()
            if not cname:
                continue
            r = subprocess.run(
                ["docker", "logs", "--tail", str(limit), cname],
                capture_output=True, text=True, timeout=10,
            )
            lines.extend((r.stdout + r.stderr).splitlines())
    except Exception as e:
        _agent_log(f"[get_logs_tail] Error: {e}")
    return {"lines": lines[-limit:]}


async def cmd_get_stats(client, state, main_id, payload, headers):
    """Aggregate stats across all replica containers for this app on this node."""
    resp = await client.get(
        f"{_LOCAL_API_BASE}/api/apps/{main_id}/replicas/aggregate-stats",
        headers=headers, timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


async def cmd_docker_cleanup(client, state, main_id, payload, headers):
    """Remove superseded images and trim the build cache on this node."""
    return await asyncio.to_thread(containers_cleanup.cleanup_docker, bool(payload.get("aggressive")))


async def cmd_get_replica_stats(client, state, main_id, payload, headers):
    replica_id = payload.get("replica_id")
    if replica_id is None:
        return {"status": "stopped", "docker": True, "error": "missing replica_id"}
    resp = await client.get(
        f"{_LOCAL_API_BASE}/api/apps/{main_id}/replicas/{int(replica_id)}/stats-remote",
        headers=headers,
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


async def cmd_get_agent_logs(client, state, main_id, payload, headers):
    # server.log has everything on this node (agent + docker + …); older
    # installs only wrote node-agent.log, so fall back to that.
    server_log = os.path.expanduser("~/.cloudbase/logs/server.log")
    log_file = server_log if os.path.exists(server_log) and os.path.getsize(server_log) > 0 else AGENT_LOG_FILE
    lines = payload.get("lines") or 100
    if not os.path.exists(log_file):
        return {"lines": []}
    with open(log_file, encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()
    return {"lines": [l.rstrip() for l in all_lines[-lines:]]}


async def cmd_get_replica_logs(client, state, main_id, payload, headers):
    container_name = payload.get("container_name")
    lines = payload.get("lines") or 200
    if not container_name:
        return {"lines": [], "error": "missing container_name"}
    try:
        import subprocess as _sp
        raw = await asyncio.to_thread(
            lambda: _sp.check_output(
                ["docker", "logs", "--tail", str(lines), container_name],
                stderr=_sp.STDOUT, text=True,
            )
        )
        return {"lines": raw.splitlines()}
    except Exception as e:
        return {"lines": [], "error": str(e)}


async def cmd_cron_run(client, state, main_id, payload, headers):
    """Run one cron job on this node: make sure the image is current, run the
    container to completion and report exit code and output."""
    await _wait_local_api(client)
    local_id = await _ensure_replica_app_deployed(client, state, main_id, payload, headers)
    run_id = int(payload["run_id"])
    _agent_log(f"[cron] app={main_id} run={run_id} starting")
    result = await asyncio.to_thread(
        containers_jobs.run_job_container, int(local_id), run_id, payload.get("app_name") or "",
        containers_naming.image_name(int(local_id), payload.get("app_name") or ""), payload.get("command") or "",
        payload.get("env_vars") or {}, payload.get("docker_options"), int(payload.get("timeout_seconds") or 3600),
    )
    _agent_log(f"[cron] app={main_id} run={run_id} exit={result.get('exit_code')} timed_out={result.get('timed_out')}")
    return result


async def cmd_start_replica(client, state, main_id, payload, headers):
    # Ensure the app source exists locally on this node. The replica container
    # itself still uses the main app id for naming and logs.
    app_name = payload.get("app_name") or ""
    replica_id = payload["replica_id"]

    # The instance is only reachable over the node network: its port is published on this node's
    # node-network address, never on the public one.
    if not state.wg_ip:
        raise RuntimeError("This node is not on the node network yet (WireGuard). Run: cloudbase wireguard")

    # If the container is already running (node briefly disconnected), keep it
    # instead of doing a full stop/start cycle.
    if containers_replicas.is_replica_container_running(int(main_id), replica_id):
        _agent_log(f"[agent] replica={replica_id} container already running, keeping it")
        try:
            container_id = containers_client._get_client().containers.get(containers_naming.replica_container_name(int(main_id), replica_id)).id
        except Exception:
            container_id = None
        return {"container_id": container_id, "replica_id": replica_id, "reused": True}

    await _wait_local_api(client)
    local_id = await _ensure_replica_app_deployed(client, state, main_id, payload, headers, replica_id)

    await _report_replica_substatus(client, state, int(main_id), replica_id, "creating_container")

    body = {
        "replica_id": replica_id,
        "local_app_id": local_id,
        "app_name":   app_name,
        "internal_port": payload.get("internal_port", 8000),
        "external_port": payload["external_port"],
        "env_vars": payload.get("env_vars") or {},
        "docker_options": {**(payload.get("docker_options") or {}), "bind_host": state.wg_ip},
    }
    resp = await client.post(
        f"{_LOCAL_API_BASE}/api/apps/{main_id}/replicas/run-remote",
        json=body, headers=headers, timeout=120,
    )
    resp.raise_for_status()
    result = resp.json()

    await _report_replica_substatus(client, state, int(main_id), replica_id, "waiting")
    return result


async def cmd_stop_replica(client, state, main_id, payload, headers):
    replica_id = payload["replica_id"]

    # Use main_id directly — no local DB lookup needed for stop.
    resp = await client.delete(
        f"{_LOCAL_API_BASE}/api/apps/{main_id}/replicas/{replica_id}/stop-remote",
        headers=headers, timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


async def cmd_refresh_source(client, state, main_id, payload, _headers):
    """Download fresh source from primary and rebuild the Docker image for this architecture.
    Called automatically after a git pull on the primary node. Always re-downloads and rebuilds."""
    app_name = payload.get("app_name") or ""
    if not app_name:
        raise RuntimeError("Missing app_name in refresh_source payload")

    app_dir = await _download_and_extract_source(client, state, int(main_id), app_name, headers={"X-Node-Token": state.auth_token})
    await _build_image_local(int(main_id), app_name, app_dir, payload)
    _agent_log(f"[refresh_source] app='{app_name}' rebuilt from updated source")
    return {"ok": True, "app_dir": app_dir}
