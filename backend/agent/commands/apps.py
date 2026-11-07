import os
import asyncio
import subprocess
import time
from containers import naming as containers_naming, cleanup as containers_cleanup, jobs as containers_jobs, stats_logs as containers_stats_logs
from agent.log import AGENT_LOG_FILE, _agent_log
from agent.source import ensure_image

# ─── Command handlers ─────────────────────────────────────────────────────────
# Questions and one-off jobs. What should run on this node is not a command:
# see reconcile.py. Everything here calls the functions directly, not the local API.


async def cmd_get_logs_tail(_client, _state, main_id, payload):
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


async def cmd_get_stats(_client, _state, main_id, _payload):
    """Aggregate stats across all replica containers for this app on this node."""
    return await containers_stats_logs.app_aggregate_stats(int(main_id))


async def cmd_docker_cleanup(_client, _state, _main_id, payload):
    """Remove superseded images and trim the build cache on this node."""
    return await asyncio.to_thread(containers_cleanup.cleanup_docker, bool(payload.get("aggressive")))


async def cmd_get_replica_stats(_client, _state, main_id, payload):
    replica_id = payload.get("replica_id")
    if replica_id is None:
        return {"status": "stopped", "docker": True, "error": "missing replica_id"}
    cname = containers_naming.replica_container_name(int(main_id), int(replica_id))
    stats = await asyncio.to_thread(containers_stats_logs.get_container_stats_by_name, cname)
    if not stats or stats.get("status") != "running":
        return {"status": "stopped", "docker": True}
    stats["timestamp"] = int(time.time() * 1000)
    stats["replica_id"] = int(replica_id)
    return stats


async def cmd_get_agent_logs(_client, _state, _main_id, payload):
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


async def cmd_get_replica_logs(_client, _state, _main_id, payload):
    container_name = payload.get("container_name")
    lines = payload.get("lines") or 200
    if not container_name:
        return {"lines": [], "error": "missing container_name"}
    try:
        raw = await asyncio.to_thread(
            lambda: subprocess.check_output(
                ["docker", "logs", "--tail", str(lines), container_name],
                stderr=subprocess.STDOUT, text=True,
            )
        )
        return {"lines": raw.splitlines()}
    except Exception as e:
        return {"lines": [], "error": str(e)}


async def cmd_cron_run(client, state, main_id, payload):
    """Run one cron job on this node: make sure the image is current, run the
    container to completion and report exit code and output."""
    app_id = int(main_id)
    await ensure_image(client, state, {**payload, "app_id": app_id}, lambda _step: None)
    run_id = int(payload["run_id"])
    _agent_log(f"[cron] app={main_id} run={run_id} starting")
    result = await asyncio.to_thread(
        containers_jobs.run_job_container, app_id, run_id, payload.get("app_name") or "",
        containers_naming.image_name(app_id, payload.get("app_name") or ""), payload.get("command") or "",
        payload.get("env_vars") or {}, payload.get("docker_options"), int(payload.get("timeout_seconds") or 3600),
    )
    _agent_log(f"[cron] app={main_id} run={run_id} exit={result.get('exit_code')} timed_out={result.get('timed_out')}")
    return result
