# ── Container naming ──────────────────────────────────────────────────────────

def image_name(app_id: int) -> str:
    return f"cloudbase/app-{app_id}:latest"


def app_dir_name(app_id: int) -> str:
    """The folder of an app's source and its name in nginx: from the id, so renaming an app changes nothing here."""
    return f"app-{app_id}"


# ── Instance helpers ───────────────────────────────────────────────────────────

def instance_container_name(app_id: int, instance_id: int) -> str:
    return f"cloudbase-app-{app_id}-instance-{instance_id}"


def job_container_name(app_id: int, run_id: int) -> str:
    return f"cloudbase-cron-{app_id}-{run_id}"


# ── Networks ──────────────────────────────────────────────────────────────────

APPS_NETWORK = "cloudbase-apps"      # every app container; containers on it cannot reach each other
LEGACY_NETWORK = "cloudbase"         # the one shared network of earlier versions


def app_link_network_name(link_id: int) -> str:
    """The two apps of a link, and nobody else."""
    return f"cloudbase-link-{link_id}"


def app_alias(app_id: int) -> str:
    """The name under which an app is reached on the network of a link. From the id, so a rename changes nothing."""
    return f"app-{app_id}"


def database_container_name(database_id: int) -> str:
    return f"cloudbase-db-{database_id}"


def database_network_name(database_id: int) -> str:
    """A database and the apps linked to it, and nobody else."""
    return f"cloudbase-db-{database_id}"
