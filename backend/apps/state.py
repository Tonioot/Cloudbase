import asyncio
from typing import Optional

RESTART_READY_TIMEOUT_SECONDS = 180


RESTART_READY_POLL_SECONDS = 1


TRANSITION_HOLD_MAX_SECONDS = 300


RESTART_READY_MIN_HEALTHY_RATIO = 0.5


# New instances during rolling / blue-green deploys. Generous because some start
# commands build at boot (e.g. `next build && next start`), which takes minutes
# on small nodes like a Raspberry Pi. A crashed container fails fast regardless.
DEPLOY_HEALTH_TIMEOUT_SECONDS = 600


DEPLOY_HEALTH_PROGRESS_SECONDS = 30


_active_transition_modes: dict[int, tuple[str, float]] = {}


# App ids with a rolling / blue-green deploy in progress (one at a time per app).
_deploys_in_progress: set[int] = set()


# App ids the autoscaler is currently adding/removing a replica for.
_scaling_in_progress: set[int] = set()


def _set_active_transition_mode(app_id: int, mode: str, started_at: Optional[float] = None) -> float:
    ts = started_at if started_at is not None else asyncio.get_running_loop().time()
    _active_transition_modes[app_id] = (mode, ts)
    return ts


def _get_active_transition_mode(app_id: int) -> Optional[str]:
    active = _active_transition_modes.get(app_id)
    if not active:
        return None
    mode, started_at = active
    elapsed = asyncio.get_running_loop().time() - started_at
    if elapsed > TRANSITION_HOLD_MAX_SECONDS:
        _active_transition_modes.pop(app_id, None)
        return None
    return mode


def _clear_active_transition_mode(app_id: int) -> None:
    _active_transition_modes.pop(app_id, None)
