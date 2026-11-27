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
APPS_LAN_NETWORK = "cloudbase-apps-lan"   # the same, for apps with open access (the local network too)
APPS_CLOSED_NETWORK = "cloudbase-apps-closed"   # the same, for apps that may not connect to anything but what they are connected to

# What an app may connect to, from where it runs: everything, the internet only, or nothing but its connections
NETWORK_ACCESS = ("open", "internet", "none")
LEGACY_NETWORK = "cloudbase"         # the one shared network of earlier versions


def app_link_network_name(link_id: int) -> str:
    """The two apps of a link, and nobody else."""
    return f"cloudbase-link-{link_id}"


def project_network_name(project_id: int) -> str:
    """The apps and databases of a project whose apps reach each other."""
    return f"cloudbase-project-{project_id}"


def app_alias(app_id: int) -> str:
    """The name under which an app is reached on the network of a link. From the id, so a rename changes nothing."""
    return f"app-{app_id}"


def database_alias(database_id: int) -> str:
    """The name under which a database is reached by the apps of its project (containers/routes.py)."""
    return f"db-{database_id}"


def route_port(database_id: int | None = None, app_id: int | None = None) -> int:
    """The port of an app's or database's fixed address inside its project. It only exists in the firewall rule of the
    project's network, so it never clashes with a port anything listens on."""
    if app_id is not None:
        return 20000 + int(app_id) % 20000
    return 40000 + int(database_id) % 20000


def database_container_name(database_id: int) -> str:
    return f"cloudbase-db-{database_id}"


def database_network_name(database_id: int) -> str:
    """A database and the apps linked to it, and nobody else."""
    return f"cloudbase-db-{database_id}"
