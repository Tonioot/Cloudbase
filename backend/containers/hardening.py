"""What a container gets in tenant mode: the `hardening` the primary put in its spec (core/tenant.py).

Used for instances, cron jobs and databases, on every machine. Without hardening in the spec nothing changes.
"""
import threading
from typing import Optional

from containers import disk_area
from containers.client import _get_client

NOFILE = 65536

_runtimes: Optional[set] = None
_lock = threading.Lock()


class RuntimeMissing(RuntimeError):
    pass


def _available_runtimes() -> set:
    global _runtimes
    with _lock:
        if _runtimes is None:
            _runtimes = set((_get_client().info().get("Runtimes") or {}).keys())
        return _runtimes


def forget_runtimes() -> None:
    """Ask Docker again next time (after a runtime was installed)."""
    global _runtimes
    with _lock:
        _runtimes = None


def check_runtime(runtime: str) -> None:
    if runtime and runtime not in _available_runtimes():
        forget_runtimes()
        if runtime not in _available_runtimes():
            raise RuntimeMissing(f"The sandbox runtime '{runtime}' is not installed on this machine "
                                 "(run scripts/setup-gvisor.sh, or set tenant.runtime to \"\")")


def apply(run_kwargs: dict, hardening: Optional[dict]) -> None:
    """Add the hardening to docker run arguments. Raises RuntimeMissing when the sandbox runtime is not here: a
    container that should be sandboxed is never started without it."""
    if not hardening:
        return
    run_kwargs["cap_drop"] = ["ALL"]
    run_kwargs["cap_add"] = list(hardening.get("cap_add") or [])
    run_kwargs["pids_limit"] = int(hardening.get("pids_limit") or 512)
    run_kwargs["ulimits"] = [{"Name": "nofile", "Soft": NOFILE, "Hard": NOFILE}]     # docker-py takes a dict too
    security = list(run_kwargs.get("security_opt") or [])
    if "no-new-privileges:true" not in security:
        security.append("no-new-privileges:true")
    run_kwargs["security_opt"] = security
    runtime = str(hardening.get("runtime") or "")
    if runtime:
        check_runtime(runtime)
        run_kwargs["runtime"] = runtime
    if int(hardening.get("disk_mb") or 0) > 0 and disk_area.limit_supported(lambda: _get_client().info()):      # elsewhere: no limit
        run_kwargs["storage_opt"] = {"size": f"{int(hardening['disk_mb'])}M"}
