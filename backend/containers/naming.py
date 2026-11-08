import re

# ── Container naming ──────────────────────────────────────────────────────────

def container_name(app_id: int) -> str:
    """The container an app had before it ran as instances; only used to clean such a container up."""
    return f"cloudbase-app-{app_id}"


def image_name(app_id: int, app_name: str) -> str:
    safe = re.sub(r"[^a-z0-9_-]", "-", app_name.lower())
    return f"cloudbase/{safe}-{app_id}:latest"


# ── Replica helpers ───────────────────────────────────────────────────────────

def replica_container_name(app_id: int, replica_id: int) -> str:
    return f"cloudbase-app-{app_id}-replica-{replica_id}"


def job_container_name(app_id: int, run_id: int) -> str:
    return f"cloudbase-cron-{app_id}-{run_id}"
