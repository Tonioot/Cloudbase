from core.models import Application
from core.env_crypto import decrypt_env

def _image_port(app: Application) -> int:
    """The port the image is built for; 0 for workers and cron jobs (no EXPOSE)."""
    return 0 if app.no_web else (app.port or 8000)


def _image_build_kwargs(app: Application) -> dict:
    """Extra dm.build_image kwargs: the app's build step and env vars for it."""
    return {
        "build_command": app.build_command or "",
        "build_env": decrypt_env(app.env_vars or ""),
    }


def _dockerfile_build_kwargs(app: Application) -> dict:
    """Extra dm.ensure_dockerfile kwargs, matching _image_build_kwargs."""
    return {
        "build_command": app.build_command or "",
        "build_env_keys": list(decrypt_env(app.env_vars or "").keys()),
    }

