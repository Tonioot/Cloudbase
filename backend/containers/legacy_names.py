"""ONE-OFF MIGRATION, to be deleted once every server has run it.

Apps used to be known by their name: the source lived in ~/.cloudbase/apps/<name>, the Docker image was called
cloudbase/<name>-<id>, the nginx config <name>. Now all three come from the id (app-<id>), so an app can be
renamed. This moves what an existing server has to the new names, and does nothing when that is done.
"""
import logging
import os
import re

from containers import naming as containers_naming
from containers.client import _get_client
from containers.processes import APPS_BASE_DIR, get_app_dir

log = logging.getLogger("cloudbase.migrate")


def old_image_name(app_id: int, app_name: str) -> str:
    return f"cloudbase/{re.sub(r'[^a-z0-9_-]', '-', app_name.lower())}-{app_id}:latest"


def old_app_dir(app_name: str) -> str:
    return os.path.join(APPS_BASE_DIR, re.sub(r"[^a-zA-Z0-9_-]", "_", app_name))


def adopt(app_id: int, app_name: str) -> list[str]:
    """Move the source folder and the image of one app to their new names. Returns what was done. Blocking."""
    done = []
    old_dir, new_dir = old_app_dir(app_name), get_app_dir(app_id)
    if old_dir != new_dir and os.path.isdir(old_dir) and not os.path.exists(new_dir):
        os.rename(old_dir, new_dir)
        done.append(f"source folder {os.path.basename(old_dir)} -> {os.path.basename(new_dir)}")

    old_image, new_image = old_image_name(app_id, app_name), containers_naming.image_name(app_id)
    try:
        client = _get_client()
        images = client.images
        try:
            images.get(new_image)
        except Exception:
            try:
                images.get(old_image).tag(new_image)       # the same image under its new name; running containers keep it
                done.append(f"image {old_image} -> {new_image}")
            except Exception:
                return done                                # no old image either: nothing to move
        if old_image != new_image:
            try:
                images.remove(old_image)                   # only the old name goes; the image stays under the new one
            except Exception:
                pass
    except Exception as exc:
        log.warning("[migrate] image of app %s not moved: %s", app_id, exc)
    return done
