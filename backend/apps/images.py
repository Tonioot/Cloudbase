from core.models import Application
from dbs import links as db_links
from core import projects as core_projects

def _image_port(app: Application) -> int:
    """The port the image is built for; 0 for workers and cron jobs (no EXPOSE)."""
    return 0 if app.no_web else (app.port or 8000)


def _image_build_kwargs(app: Application) -> dict:
    """Extra dm.build_image kwargs: the app's build step and env vars for it."""
    return {
        "build_command": app.build_command or "",
        "build_env": core_projects.app_env(app),
        "linked_databases": db_links.runtime_options(app.id).get("linked_databases"),
    }


def _dockerfile_build_kwargs(app: Application) -> dict:
    """Extra dm.ensure_dockerfile kwargs, matching _image_build_kwargs."""
    return {
        "build_command": app.build_command or "",
        "build_env_keys": list(core_projects.app_env(app).keys()),
    }

