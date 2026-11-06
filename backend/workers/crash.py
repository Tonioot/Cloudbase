import asyncio
import time as _time
from sqlalchemy import select
from core.database import AsyncSessionLocal
from core.models import Application, Node as _Node, ApplicationReplica
from core.env_crypto import decrypt_env
from containers import processes as pm, manager as dm
from core import config as _cfg
from apps.runtime import _derive_app_status_from_instances
from apps.nginx_sync import _write_app_nginx_config as _wanc, _has_public_nginx_domain as _hpnd
from workers.recovery import _docker_runtime_options

# Restart-loop protection: max 5 restarts per 60s per app
_restart_history: dict[int, list[float]] = {}


MAX_RESTARTS_PER_WINDOW = _cfg.get_limit("max_restarts_per_window")


RESTART_WINDOW_SECONDS = _cfg.get_limit("restart_window_seconds")


# ── Crash monitor ─────────────────────────────────────────────────────────────
async def _crash_monitor():
    await asyncio.sleep(5)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                local_node_result = await db.execute(select(_Node).where(_Node.is_local == True))
                local_node_obj = local_node_result.scalar_one_or_none()

                # Monitor local replica containers
                rep_result = await db.execute(
                    select(ApplicationReplica).where(ApplicationReplica.status == "running")
                )
                replicas = rep_result.scalars().all()
                node_map: dict = {}
                for replica in replicas:
                    r_node_id = replica.node_id
                    if r_node_id not in node_map:
                        nr = await db.execute(select(_Node).where(_Node.id == r_node_id))
                        node_map[r_node_id] = nr.scalar_one_or_none()
                    r_node = node_map.get(r_node_id)
                    if r_node and not r_node.is_local:
                        continue  # remote replicas managed by their agent

                    alive = await asyncio.to_thread(dm.is_replica_container_running, replica.app_id, replica.id)
                    if alive:
                        continue

                    app_result = await db.execute(select(Application).where(Application.id == replica.app_id))
                    a = app_result.scalar_one_or_none()
                    if not a:
                        continue

                    policy = a.restart_policy or "no"
                    if policy == "no":
                        replica.status = "stopped"
                        pm._push_line(a.id, f"⚠ Instance {replica.id} exited.")
                        await db.commit()
                        try:
                            if local_node_obj and _hpnd(a):
                                await _wanc(a, db, local_node_obj)
                        except Exception:
                            pass
                        continue

                    now = _time.time()
                    key = (a.id, replica.id)
                    history = _restart_history.setdefault(key, [])
                    history[:] = [t for t in history if now - t < RESTART_WINDOW_SECONDS]

                    if len(history) >= MAX_RESTARTS_PER_WINDOW:
                        replica.status = "error"
                        replica.last_error = f"Crashed {MAX_RESTARTS_PER_WINDOW}× in {RESTART_WINDOW_SECONDS}s"
                        pm._push_line(a.id, f"✖ Instance {replica.id} crashed {MAX_RESTARTS_PER_WINDOW}× — giving up.")
                        await db.commit()
                        try:
                            if local_node_obj and _hpnd(a):
                                await _wanc(a, db, local_node_obj)
                        except Exception:
                            pass
                        continue

                    history.append(now)
                    attempt = len(history)
                    pm._push_line(a.id, f"⟳ Instance {replica.id} exited — restarting (attempt {attempt}/{MAX_RESTARTS_PER_WINDOW})…")
                    await asyncio.sleep(min(2 ** attempt, 30))

                    try:
                        env_vars = decrypt_env(a.env_vars or "")
                        cid = await asyncio.to_thread(
                            pm.start_docker_replica,
                            a.id, replica.id, a.name,
                            a.port or 8000, replica.external_port or a.port or 8000,
                            env_vars, _docker_runtime_options(a),
                        )
                        replica.status = "running"
                        replica.container_id = cid
                        replica.last_error = None
                        pm._push_line(a.id, f"✓ Instance {replica.id} restarted ({cid[:12]}).")
                    except Exception as e:
                        replica.status = "error"
                        replica.last_error = str(e)
                        pm._push_line(a.id, f"✖ Instance {replica.id} restart failed: {e}")

                    # Sync app status from all its replicas
                    all_rep_result = await db.execute(
                        select(ApplicationReplica).where(ApplicationReplica.app_id == a.id)
                    )
                    a.status = _derive_app_status_from_instances(all_rep_result.scalars().all())
                    await db.commit()
                    # If this restart attempt failed, the backend is dead — remove it from nginx upstream
                    if replica.status in ("error", "stopped"):
                        try:
                            if local_node_obj and _hpnd(a):
                                await _wanc(a, db, local_node_obj)
                        except Exception:
                            pass
        except asyncio.CancelledError:
            return
        except Exception:
            pass
        await asyncio.sleep(5)
