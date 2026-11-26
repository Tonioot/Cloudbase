import os
import asyncio
import subprocess
import tempfile
from core import power, selfupdate
from containers import naming as containers_naming, cleanup as containers_cleanup, jobs as containers_jobs, output_log, static_export
from agent.log import AGENT_LOG_FILE, _agent_log
from agent.source import ensure_image
from agent.commands.databases import _upload_base

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


async def cmd_reboot(_client, _state, _main_id, _payload):
    """Restart this node's server (needs the root helper; see core/power.py)."""
    return await asyncio.to_thread(power.reboot)


async def cmd_logs_search(_client, _state, _main_id, payload):
    """Search the app output this node keeps (see containers/output_log.py)."""
    lines = await asyncio.to_thread(
        output_log.search, int(payload["app_id"]), since_ns=int(payload["since_ns"]),
        until_ns=int(payload["until_ns"]) if payload.get("until_ns") else None, q=str(payload.get("q") or ""),
        errors_only=bool(payload.get("errors_only")),
        instance_id=int(payload["instance_id"]) if payload.get("instance_id") else None,
        limit=int(payload.get("limit") or 500))
    return {"lines": lines, "usage": output_log.usage(int(payload["app_id"]))}


async def cmd_log_settings(_client, _state, _main_id, payload):
    """The primary hands out how long app output is kept; this node keeps pruning with it on its own."""
    return await asyncio.to_thread(output_log.set_settings, payload or {})


async def cmd_logs_forget_app(_client, _state, _main_id, payload):
    await asyncio.to_thread(output_log.forget_app, int(payload["app_id"]))
    return {"ok": True}


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


_SITE_TMP = os.path.expanduser("~/.cloudbase/site-tmp")


async def cmd_static_build(client, state, main_id, payload):
    """Build a static site in this node's sandbox, as the image of an instance would be, take the site out of the image
    without starting it, and hand it to the primary, which serves it (apps/static_sites.py). Nothing stays here but
    the image, which the next build reuses."""
    app_id = int(main_id)
    await ensure_image(client, state, {**payload, "app_id": app_id}, lambda _step: None)
    os.makedirs(_SITE_TMP, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix=f"site-{app_id}-", suffix=".tar.gz", dir=_SITE_TMP)
    os.close(fd)
    try:
        size = await asyncio.to_thread(static_export.export_site, containers_naming.image_name(app_id), path,
                                       int(payload.get("max_bytes") or (1 << 30)))
        base = await _upload_base(client, state, payload.get("hub_url"))
        url = f"{base}/api/nodes/agent/apps/{app_id}/static-site/{payload['build_id']}"

        async def body():
            with open(path, "rb") as f:
                while True:
                    chunk = await asyncio.to_thread(f.read, 1 << 20)
                    if not chunk:
                        return
                    yield chunk

        resp = await client.post(url, content=body(), headers={"X-Node-Token": state.auth_token}, timeout=None)
        if resp.status_code != 200:
            raise RuntimeError(f"the primary did not take the site (HTTP {resp.status_code}: {resp.text[:300]})")
        _agent_log(f"[static] app={app_id} site handed in ({size // 1024} KB)")
        return {"size_bytes": size}
    finally:
        if os.path.exists(path):
            os.remove(path)
