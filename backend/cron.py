"""Cron jobs: apps that run their command on a schedule instead of staying up.

The source and image are the same as for any app (and are deployed the same
way); scheduler() starts a run when a slot comes up. A run is one container
on the job's node that does its work and exits — Cloudbase records the exit
code, duration and output, and removes the container.

Missed slots: when Cloudbase was down, the latest slot is still run if it was
at most CATCH_UP_SECONDS ago, otherwise it is reported as missed. A slot that
comes up while the previous run is still going is skipped and reported.
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import delete, select

import cronexpr
import docker_manager as dm
import notify
import process_manager as pm
from database import AsyncSessionLocal
from env_crypto import decrypt_env
from models import Application, CronRun, Node

log = logging.getLogger("cloudbase.cron")

SCHEDULER_SECONDS = 10
CATCH_UP_SECONDS = 600
BUILD_MARGIN_SECONDS = 1800      # a run on another node may have to build the image first
RUNS_KEPT_PER_APP = 200
TIMEOUT_CHOICES = [60, 300, 900, 1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600, 24 * 3600]

_running: dict[int, int] = {}    # app id → id of the run in progress


def _utcnow() -> datetime:
    return datetime.utcnow()


# ── Schedules ─────────────────────────────────────────────────────────────────

def check_schedule(schedule: str, tz_name: Optional[str]) -> cronexpr.Cron:
    """Parse and make sure it runs at all; raises HTTPException(400) otherwise."""
    try:
        cron = cronexpr.parse(schedule)
        cronexpr.next_run(cron, _utcnow(), tz_name)
        return cron
    except cronexpr.CronError as e:
        raise HTTPException(400, str(e))


def preview(schedule: str, tz_name: Optional[str], count: int = 5) -> dict:
    cron = check_schedule(schedule, tz_name)
    runs = cronexpr.next_runs(cron, _utcnow(), tz_name, count)
    return {"schedule": cron.expr, "description": cronexpr.describe(cron),
            "next_runs": [r.isoformat() + "Z" for r in runs]}


def status_of(app: Application) -> str:
    if app.id in _running:
        return "running"
    return "scheduled" if app.cron_enabled else "paused"


def info(app: Application) -> Optional[dict]:
    """The cron part of an app, for the panel."""
    if not app.cron_schedule:
        return None
    description, next_at = app.cron_schedule, None
    try:
        cron = cronexpr.parse(app.cron_schedule)
        description = cronexpr.describe(cron)
        if app.cron_enabled:
            next_at = cronexpr.next_run(cron, _utcnow(), app.cron_timezone).isoformat() + "Z"
    except cronexpr.CronError:
        pass
    return {
        "schedule": app.cron_schedule, "timezone": app.cron_timezone or "UTC",
        "description": description, "enabled": bool(app.cron_enabled),
        "timeout_seconds": app.cron_timeout_seconds or 3600, "next_run_at": next_at,
        "running_run_id": _running.get(app.id),
    }


def run_dict(run: CronRun, with_output: bool = False) -> dict:
    iso = lambda d: d.isoformat() + "Z" if d else None  # noqa: E731
    out = {
        "id": run.id, "trigger": run.trigger, "status": run.status, "exit_code": run.exit_code,
        "error": run.error, "actor": run.actor, "node_id": run.node_id,
        "scheduled_for": iso(run.scheduled_for), "started_at": iso(run.started_at), "finished_at": iso(run.finished_at),
        "duration_seconds": round((run.finished_at - run.started_at).total_seconds(), 1) if run.finished_at else None,
    }
    if with_output:
        out["output"] = run.output or ""
    return out


async def _set_app_status(app_id: int) -> None:
    async with AsyncSessionLocal() as db:
        app = await db.get(Application, app_id)
        if app is not None and app.cron_schedule:
            app.status = status_of(app)
            await db.commit()


async def set_enabled(app_id: int, enabled: bool, actor: str) -> dict:
    """Pause or resume the schedule. A resumed job starts at the next slot."""
    from audit import log_audit
    async with AsyncSessionLocal() as db:
        app = await db.get(Application, app_id)
        if app is None or not app.cron_schedule:
            raise HTTPException(404, "Cron job not found")
        app.cron_enabled = enabled
        app.cron_last_slot = None
        app.status = status_of(app)
        await log_audit(db, "cron.resume" if enabled else "cron.pause", actor=actor, app_id=app_id,
                        detail={"name": app.name})
        await db.commit()
        pm._push_line(app_id, f"[Cron] Schedule {'resumed' if enabled else 'paused'} by {actor}")
        return {"status": app.status, "cron": info(app)}


async def update_settings(app_id: int, actor: str, *, schedule: Optional[str] = None, tz_name: Optional[str] = None,
                          timeout_seconds: Optional[int] = None, node_id: Optional[int] = None,
                          clear_node: bool = False) -> dict:
    from audit import log_audit
    async with AsyncSessionLocal() as db:
        app = await db.get(Application, app_id)
        if app is None or not app.cron_schedule:
            raise HTTPException(404, "Cron job not found")
        new_schedule = " ".join(schedule.split()) if schedule is not None else app.cron_schedule
        new_tz = (tz_name or None) if tz_name is not None else app.cron_timezone
        check_schedule(new_schedule, new_tz)
        if timeout_seconds is not None and timeout_seconds not in TIMEOUT_CHOICES:
            raise HTTPException(400, f"timeout_seconds must be one of {TIMEOUT_CHOICES}")
        if node_id is not None:
            node = await db.get(Node, node_id)
            if node is None or not node.enabled:
                raise HTTPException(400, "Selected node is not available")
            app.node_id = None if node.is_local else node.id
        elif clear_node:
            app.node_id = None
        if (new_schedule, new_tz) != (app.cron_schedule, app.cron_timezone):
            app.cron_last_slot = None  # start counting from now, don't catch up old slots
        app.cron_schedule, app.cron_timezone = new_schedule, new_tz
        if timeout_seconds is not None:
            app.cron_timeout_seconds = timeout_seconds
        await log_audit(db, "cron.settings", actor=actor, app_id=app_id, detail={
            "schedule": app.cron_schedule, "timezone": app.cron_timezone,
            "timeout_seconds": app.cron_timeout_seconds, "node_id": app.node_id,
        })
        await db.commit()
        return info(app)


# ── Running ───────────────────────────────────────────────────────────────────

async def start_run(app_id: int, trigger: str, actor: str, slot: Optional[datetime] = None) -> dict:
    """Start a run in the background and return it. 409 when one is going."""
    if app_id in _running:
        raise HTTPException(409, "This job is already running")
    async with AsyncSessionLocal() as db:
        app = await db.get(Application, app_id)
        if app is None or not app.cron_schedule:
            raise HTTPException(404, "Cron job not found")
        run = CronRun(app_id=app_id, trigger=trigger, status="running", scheduled_for=slot,
                      actor=actor, node_id=app.node_id)
        db.add(run)
        await db.commit()
        await db.refresh(run)
        _running[app_id] = run.id
        result = run_dict(run)
    await _set_app_status(app_id)
    asyncio.create_task(_execute(run.id))
    return result


async def _ensure_local_image(app: Application, push) -> str:
    from routers import applications as apps
    img = dm.image_name(app.id, app.name)
    desired = await asyncio.to_thread(apps._refresh_app_source_revision, app)
    if dm.image_exists(img) and (not desired or app.image_revision == desired):
        return img
    if not app.working_dir:
        raise RuntimeError("No source yet — deploy the job first")
    push("[Cron] Building image…")
    await asyncio.to_thread(
        dm.build_image, app.id, app.name, app.working_dir, lambda _aid, line: push(line),
        app.app_type or "unknown", app.start_command or "", apps._image_port(app),
        **apps._image_build_kwargs(app),
    )
    async with AsyncSessionLocal() as db:
        fresh = await db.get(Application, app.id)
        if fresh is not None:
            fresh.docker_image = img
            fresh.image_revision = desired or fresh.image_revision
            await db.commit()
    return img


async def _run_local(app: Application, run_id: int, push) -> dict:
    from routers import applications as apps
    img = await _ensure_local_image(app, push)
    options = apps._docker_runtime_options(app)
    return await asyncio.to_thread(
        dm.run_job_container, app.id, run_id, app.name, img, app.start_command or "",
        decrypt_env(app.env_vars or ""), options, app.cron_timeout_seconds or 3600,
        lambda line: pm._push_line(app.id, line),
    )


async def _run_remote(app: Application, node: Node, run_id: int) -> dict:
    import json
    from routers import applications as apps
    from routers.nodes import queue_node_command, wait_for_node_command
    if not (node.enabled and node.status == "online"):
        raise RuntimeError(f"Node '{node.name}' is offline")
    payload = apps._remote_replica_command_payload(app, decrypt_env(app.env_vars or ""), 0)
    payload.update({"run_id": run_id, "command": app.start_command or "",
                    "timeout_seconds": app.cron_timeout_seconds or 3600})
    async with AsyncSessionLocal() as db:
        cmd = await queue_node_command(db, node_id=node.id, command_type="cron_run", payload=payload, app_id=app.id)
        done = await wait_for_node_command(
            db, cmd.id, timeout_seconds=(app.cron_timeout_seconds or 3600) + BUILD_MARGIN_SECONDS)
    if done.status != "done":
        if "Unsupported command type" in (done.error_message or ""):
            raise RuntimeError(f"Node '{node.name}' runs an older Cloudbase; update it to run cron jobs there")
        raise RuntimeError(done.error_message or f"The run failed on node '{node.name}'")
    return json.loads(done.result or "{}")


async def _execute(run_id: int) -> None:
    async with AsyncSessionLocal() as db:
        run = await db.get(CronRun, run_id)
        app = await db.get(Application, run.app_id) if run else None
        node = await db.get(Node, app.node_id) if app and app.node_id else None
    if run is None or app is None:
        _running.pop(run.app_id if run else -1, None)
        return
    app_id = app.id

    def push(line: str) -> None:
        pm._push_line(app_id, line)

    where = f" on {node.name}" if node is not None and not node.is_local else ""
    push(f"[Cron] Run #{run_id} started{where} ({'manual' if run.trigger == 'manual' else 'scheduled'})")
    result, error = None, None
    try:
        if node is not None and not node.is_local:
            result = await _run_remote(app, node, run_id)
        else:
            result = await _run_local(app, run_id, push)
    except HTTPException as e:
        error = str(e.detail)
    except Exception as e:
        error = str(e) or type(e).__name__
        if "fetching server API version" in error:
            error = "Docker isn't running or can't be reached on this server"

    exit_code = result.get("exit_code") if result else None
    if error:
        status = "failed"
    elif result.get("timed_out"):
        status = "timeout"
    else:
        status = "success" if exit_code == 0 else "failed"
    async with AsyncSessionLocal() as db:
        run = await db.get(CronRun, run_id)
        if run is not None:
            run.status, run.exit_code, run.error = status, exit_code, (error or None) and error[:2000]
            run.output = (result or {}).get("output") or None
            run.finished_at = _utcnow()
            await db.commit()
            duration = (run.finished_at - run.started_at).total_seconds()
        else:
            duration = 0
    _running.pop(app_id, None)
    await _set_app_status(app_id)

    took = f"{duration:.1f}s" if duration < 90 else f"{duration / 60:.1f} min"
    if status == "success":
        push(f"[Cron] Run #{run_id} succeeded in {took}")
        await notify.resolve(f"cron.failed:{app_id}", f"Cron job {app.name} succeeded again",
                             f"Run #{run_id} finished in {took}.")
        return
    what = {"timeout": f"timed out after {took}", "failed": "failed"}[status]
    detail = error or (f"Exit code {exit_code}." if exit_code is not None else "")
    push(f"[Cron] Run #{run_id} {what}" + (f": {detail}" if detail else ""))
    tail = "\n".join(((result or {}).get("output") or "").strip().splitlines()[-12:])
    await notify.emit("cron.failed", f"Cron job {app.name} {what}",
                      "\n".join(x for x in [detail, tail] if x), app_id=app_id,
                      link=f"/app?id={app_id}", key=f"cron.failed:{app_id}",
                      severity="critical" if status == "timeout" or error else None)


# ── Scheduling ────────────────────────────────────────────────────────────────

async def _missed(app: Application, slot: datetime, reason: str) -> None:
    log.warning("[cron] %s missed its %s run: %s", app.name, slot.isoformat(), reason, extra={"app_id": app.id})
    pm._push_line(app.id, f"[Cron] Skipped the run of {slot:%Y-%m-%d %H:%M} UTC: {reason}")
    await notify.emit("cron.missed", f"Cron job {app.name} skipped a run", reason,
                      app_id=app.id, link=f"/app?id={app.id}")


async def _tick() -> None:
    now = _utcnow()
    due: list[tuple[int, datetime]] = []
    missed: list[tuple[int, datetime, str]] = []
    async with AsyncSessionLocal() as db:
        apps = (await db.execute(select(Application).where(Application.cron_schedule.is_not(None)))).scalars().all()
        for app in apps:
            if not app.cron_enabled or not app.working_dir:
                continue
            try:
                cron = cronexpr.parse(app.cron_schedule)
            except cronexpr.CronError:
                continue
            if app.cron_last_slot is None:
                app.cron_last_slot = now  # new or resumed: start from the next slot
                continue
            try:
                slot = cronexpr.next_run(cron, app.cron_last_slot, app.cron_timezone)
                if slot > now:
                    continue
                latest = slot
                for _ in range(100_000):  # skip to the newest slot that has come up
                    following = cronexpr.next_run(cron, latest, app.cron_timezone)
                    if following > now:
                        break
                    latest = following
            except cronexpr.CronError:
                continue
            app.cron_last_slot = latest
            if (now - latest).total_seconds() > CATCH_UP_SECONDS:
                missed.append((app.id, latest, "Cloudbase wasn't running at that time."))
            elif app.id in _running:
                missed.append((app.id, latest, f"Run #{_running[app.id]} was still going."))
            else:
                due.append((app.id, latest))
        await db.commit()
        names = {a.id: a for a in apps}
    for app_id, slot, reason in missed:
        await _missed(names[app_id], slot, reason)
    for app_id, slot in due:
        try:
            await start_run(app_id, "schedule", "scheduler", slot=slot)
        except HTTPException as e:
            log.warning("[cron] could not start app %s: %s", app_id, e.detail)


async def _cleanup() -> None:
    async with AsyncSessionLocal() as db:
        app_ids = (await db.execute(select(Application.id).where(Application.cron_schedule.is_not(None)))).scalars().all()
        for app_id in app_ids:
            keep = (await db.execute(
                select(CronRun.id).where(CronRun.app_id == app_id)
                .order_by(CronRun.id.desc()).offset(RUNS_KEPT_PER_APP).limit(1)
            )).scalar_one_or_none()
            if keep:
                await db.execute(delete(CronRun).where(CronRun.app_id == app_id, CronRun.id <= keep))
        await db.commit()


async def _recover() -> None:
    """After a restart: runs that were going are lost; so are their containers."""
    async with AsyncSessionLocal() as db:
        stale = (await db.execute(select(CronRun).where(CronRun.status == "running"))).scalars().all()
        for run in stale:
            run.status, run.finished_at = "failed", _utcnow()
            run.error = "Interrupted: Cloudbase restarted during the run"
        apps = (await db.execute(select(Application).where(Application.cron_schedule.is_not(None)))).scalars().all()
        for app in apps:
            app.status = status_of(app)
        await db.commit()
    try:
        for name in await asyncio.to_thread(dm.list_job_containers):
            await asyncio.to_thread(dm.remove_container_by_name, name)
    except Exception as e:
        log.debug("could not clean up job containers: %s", e)


async def scheduler() -> None:
    """Start cron runs when their slot comes up (primary only)."""
    await _recover()
    await asyncio.sleep(5)
    last_cleanup = 0.0
    loop = asyncio.get_running_loop()
    while True:
        try:
            await _tick()
            if loop.time() - last_cleanup > 3600:
                last_cleanup = loop.time()
                await _cleanup()
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.warning("[cron] scheduler error: %s", e)
        await asyncio.sleep(SCHEDULER_SECONDS)
