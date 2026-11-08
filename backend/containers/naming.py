# ── Container naming ──────────────────────────────────────────────────────────

def image_name(app_id: int) -> str:
    return f"cloudbase/app-{app_id}:latest"


def app_dir_name(app_id: int) -> str:
    """The folder of an app's source and its name in nginx: from the id, so renaming an app changes nothing here."""
    return f"app-{app_id}"


# ── Replica helpers ───────────────────────────────────────────────────────────

def replica_container_name(app_id: int, replica_id: int) -> str:
    return f"cloudbase-app-{app_id}-replica-{replica_id}"


def job_container_name(app_id: int, run_id: int) -> str:
    return f"cloudbase-cron-{app_id}-{run_id}"
