import logging
import os
import subprocess
from typing import Optional

log = logging.getLogger("cloudbase.docker")


# ── BuildKit ──────────────────────────────────────────────────────────────────
# BuildKit skips the intermediate container the classic builder starts for every
# instruction (each ARG/ENV/WORKDIR cost a container + layer commit), builds
# independent stages in parallel and supports persistent cache mounts, so package
# downloads survive across builds. It needs the docker CLI with the buildx plugin.
# Set CLOUDBASE_BUILDKIT=0 to force the classic builder.

_buildkit_available: Optional[bool] = None


def buildkit_available() -> bool:
    global _buildkit_available
    if _buildkit_available is not None:
        return _buildkit_available
    result = False
    if os.environ.get("CLOUDBASE_BUILDKIT", "1").strip().lower() not in ("0", "false", "no", "off"):
        try:
            result = subprocess.run(
                ["docker", "buildx", "version"],
                capture_output=True, timeout=15,
            ).returncode == 0
        except Exception:
            result = False
    _buildkit_available = result
    log.info("[docker] BuildKit %s", "enabled" if result else "unavailable, using the classic builder")
    return result


_limits_supported: Optional[bool] = None


def resource_limits_supported() -> bool:
    """Can `docker build` limit what a build step uses (--resource: buildx 0.35 and newer)?"""
    global _limits_supported
    if _limits_supported is None:
        try:
            done = subprocess.run(["docker", "build", "--help"], capture_output=True, text=True, timeout=15)
            _limits_supported = "--resource" in done.stdout
        except Exception:
            return False
    return _limits_supported


def resource_limit_args(cpu: Optional[float], memory_mb: Optional[int]) -> list[str]:
    """`docker build` arguments that hold every build step to these limits; swap of the same size softens a short peak."""
    out: list[str] = []
    if memory_mb:
        out += ["--resource", f"memory={int(memory_mb)}m", "--resource", f"memory-swap={2 * int(memory_mb)}m"]
    if cpu:
        out += ["--resource", "cpu-period=100000", "--resource", f"cpu-quota={max(1000, int(float(cpu) * 100000))}"]
    return out


_BUILD_SECRET_ID = "buildenv"


def _run(cmd: str, cache_dirs=()) -> str:
    """`RUN cmd`, with persistent cache mounts when BuildKit is available."""
    mounts = "".join(f"--mount=type=cache,target={d} " for d in cache_dirs) if buildkit_available() else ""
    return f"RUN {mounts}{cmd}\n"
