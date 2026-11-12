import os
import asyncio
import subprocess
from core import selfupdate
from containers import naming as containers_naming, cleanup as containers_cleanup, jobs as containers_jobs
from agent.log import AGENT_LOG_FILE, _agent_log
from agent.source import ensure_image

# ─── Command handlers ─────────────────────────────────────────────────────────
# Questions and one-off jobs. What should run on this node is not a command:
# see reconcile.py. Everything here calls the functions directly, not the local API.


async def cmd_docker_cleanup(_client, _state, _main_id, payload):
    """Remove superseded images and trim the build cache on this node."""
    return await asyncio.to_thread(containers_cleanup.cleanup_docker, bool(payload.get("aggressive")))


async def cmd_self_update(_client, _state, _main_id, _payload):
    """Update this node's Cloudbase from Git and restart it (no root needed; see core/selfupdate.py)."""
    result = await asyncio.to_thread(selfupdate.update)
    if result["status"] == "updated":
        selfupdate.restart_soon(graceful=False)   # after the answer has been sent
    return result


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


async def cmd_get_instance_logs(_client, _state, _main_id, payload):
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
        containers_jobs.run_job_container, app_id, run_id,
        containers_naming.image_name(app_id), payload.get("command") or "",
        payload.get("env_vars") or {}, payload.get("docker_options"), int(payload.get("timeout_seconds") or 3600),
    )
    _agent_log(f"[cron] app={main_id} run={run_id} exit={result.get('exit_code')} timed_out={result.get('timed_out')}")
    return result
