"""Static sites without a container: nginx on this server serves their files straight from disk.

A deploy makes the files once:
    no build command  the publish folder is copied from the source on this server; nothing of the site runs
    a build command   the build runs where code of others may run: in tenant mode on a node, in its sandbox (the node
                      builds the image as it would for an instance, takes the folder out of it without starting it and
                      uploads it here), otherwise in Docker on this server. The image is the one containers/dockerfiles.py
                      makes; containers/static_export.py takes the site out of it.
The files land in nginx/paths.site_dir(app)/<release>/, one folder per version. nginx's root points at the live one, and a
deploy switches by writing the config again (one reload: no visitor ever sees half a site). The last KEEP versions stay.

A site with a published version (static_release) has no instances: start and stop switch nginx between the site and the
downtime page, scale has nothing to do and the reservations of the nodes count nothing for it. A static site from before
keeps its instances until its next deploy, which publishes it and removes them.

Safe by construction: only plain files and folders are taken (no links, devices or names that leave the folder), size and
number of files are capped (limits.static_site_mb, limits.static_site_files), files are 0644 and folders 0755, and nginx
refuses symlinks on top of that (nginx/config.py). A node may only fetch the source of the app it builds, and only upload
the one site it was asked for, once.
"""
import asyncio
import logging
import os
import re
import secrets
import shutil
import stat
import tarfile
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import AsyncIterator, Callable, Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps import nginx_sync
from apps.images import _image_build_kwargs, _image_port
from apps.rollout import remove_instances
from apps.runtime import _load_app_instances, _remote_instance_command_payload
from containers import build as containers_build, dockerfiles as containers_dockerfiles, processes as pm, static_export
from core import config as _cfg, projects as core_projects, tenant
from core.models import Application, Node
from mesh import addressing
from nginx import paths as nginx_paths
from nodes import commands as node_commands

log = logging.getLogger("cloudbase.static")

KEEP = 3                          # published versions kept per site (the live one always)
BUILD_TIMEOUT_SECONDS = 1800      # a build on a node: getting the source, the image, the upload
_SKIP_AT_TOP = {".git"}


def serves(app: Application) -> bool:
    """Whether nginx serves this app's files itself (a static site with a published version)."""
    return app.app_type == "static" and bool(app.static_release)


def max_bytes() -> int:
    return max(1, _cfg.get_limit("static_site_mb")) * 1024 * 1024


def max_files() -> int:
    return max(1, _cfg.get_limit("static_site_files"))


class _Count:
    """Files and bytes taken so far, refused past the limits."""

    def __init__(self):
        self.files, self.bytes, self.max_files, self.max_bytes = 0, 0, max_files(), max_bytes()

    def add(self, size: int) -> None:
        self.files += 1
        self.bytes += max(0, size)
        if self.files > self.max_files:
            raise HTTPException(422, f"The site has more than {self.max_files} files")
        if self.bytes > self.max_bytes:
            raise HTTPException(422, f"The site is larger than {self.max_bytes // (1024 * 1024)} MB")


# ── Builds on a node: what it may fetch and send ─────────────────────────────

@dataclass
class _Build:
    app_id: int
    node_id: int
    path: str
    received: bool = False


_builds: dict[str, _Build] = {}


def node_may_fetch(app_id: int, node_id: int) -> bool:
    """A node that builds this site right now may fetch its source."""
    return any(b.app_id == app_id and b.node_id == node_id for b in _builds.values())


async def receive_upload(build_id: str, app_id: int, node_id: int, chunks: AsyncIterator[bytes]) -> int:
    """The archive of a site a node built: kept only when it is the one asked for, from that node, once and whole."""
    build = _builds.get(build_id)
    if build is None or build.app_id != app_id or build.node_id != node_id or build.received:
        raise HTTPException(403, "No build of this site is waiting for this node")
    part, total, cap = build.path + ".part", 0, max_bytes()
    try:
        with open(part, "wb") as out:
            async for chunk in chunks:
                total += len(chunk)
                if total > cap:
                    raise HTTPException(413, f"The site is larger than {cap // (1024 * 1024)} MB")
                out.write(chunk)
        os.replace(part, build.path)
        build.received = True
    finally:
        if os.path.exists(part):
            os.remove(part)
    return total


# ── Taking files: from the source, or from a built archive ───────────────────

def _generated_dockerfile(path: str) -> bool:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(len(containers_dockerfiles.GENERATED_MARKER)) == containers_dockerfiles.GENERATED_MARKER
    except OSError:
        return False


def _copy_tree(src_root: str, dest: str) -> _Count:
    """Copy the plain files and folders under src_root to dest. Links, devices and .git are left out."""
    count = _Count()
    for dirpath, dirnames, filenames in os.walk(src_root, followlinks=False):
        rel = os.path.relpath(dirpath, src_root)
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))
                       and not (rel == "." and d in _SKIP_AT_TOP)]
        target_dir = dest if rel == "." else os.path.join(dest, rel)
        os.makedirs(target_dir, exist_ok=True)
        os.chmod(target_dir, 0o755)
        for name in filenames:
            path = os.path.join(dirpath, name)
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode):
                continue
            if rel == "." and name == "Dockerfile" and _generated_dockerfile(path):
                continue                                  # Cloudbase's own, not part of the site
            count.add(st.st_size)
            target = os.path.join(target_dir, name)
            shutil.copyfile(path, target, follow_symlinks=False)
            os.chmod(target, 0o644)
    return count


def _copy_from_source(app: Application, dest: str) -> _Count:
    source = pm.app_source_dir(app)
    if not source or not os.path.isdir(source):
        raise HTTPException(400, "No source yet: deploy the site first")
    try:
        folder = containers_dockerfiles.resolve_static_dir(source, app.start_command or "", built=False)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    src = os.path.realpath(os.path.join(source, folder))
    real_source = os.path.realpath(source)
    if os.path.commonpath([real_source, src]) != real_source:
        raise HTTPException(422, "The publish directory must stay inside the repository")
    if not os.path.isdir(src):
        raise HTTPException(422, f"The publish directory '{folder}' is not in the repository. Set it to the folder with "
                                 "your index.html, or add a build command that makes it.")
    return _copy_tree(src, dest)


def _unpack(archive: str, dest: str) -> _Count:
    """Unpack a site archive (top folder: html/, as docker cp makes it) into dest: plain files and folders only."""
    count = _Count()
    os.makedirs(dest, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            name = member.name.replace("\\", "/")
            if name.startswith("/"):
                raise HTTPException(422, "The site archive has an absolute path")
            parts = PurePosixPath(name).parts[1:]            # without html/
            if not parts:
                continue
            if any(p in ("..", "") or "\x00" in p for p in parts):
                raise HTTPException(422, "The site archive has a path that leaves its folder")
            target = os.path.join(dest, *parts)
            if member.isdir():
                os.makedirs(target, exist_ok=True)
                os.chmod(target, 0o755)
            elif member.isreg():
                count.add(member.size)
                parent = os.path.dirname(target)
                os.makedirs(parent, exist_ok=True)
                os.chmod(parent, 0o755)
                src = tar.extractfile(member)
                with src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
                os.chmod(target, 0o644)
            # links, devices and fifos are left out
    return count


# ── Building ─────────────────────────────────────────────────────────────────

def _hub_url(node: Node) -> Optional[str]:
    """The panel as the node reaches it over the node network, or None without one (the agent then uses the panel)."""
    cfg = _cfg.get_wireguard()
    if not cfg.get("enabled") or not node.wg_ip:
        return None
    return f"http://{addressing.hub_ip(cfg['subnet'])}:{_cfg.get_server_port()}"


async def _pick_node(db: AsyncSession) -> Node:
    nodes = [n for n in (await db.execute(select(Node).where(Node.enabled == True))).scalars().all()  # noqa: E712
             if not n.is_local and n.status == "online" and tenant.may_run_on(n)]
    if not nodes:
        raise HTTPException(409, "No node is online to build the site: the code of a site is never built on the primary")
    return secrets.choice(nodes)


async def _build_on_node(app: Application, db: AsyncSession, push, source_revision: Optional[str], archive: str) -> None:
    node = await _pick_node(db)
    build_id = secrets.token_urlsafe(24)
    _builds[build_id] = _Build(app.id, node.id, archive)
    try:
        payload = _remote_instance_command_payload(app, core_projects.app_env(app), 0, source_revision)
        payload.update(build_id=build_id, max_bytes=max_bytes(), hub_url=_hub_url(node))
        push(f"[Static] Building the site on node '{node.name}'…")
        done = await node_commands.ask(node.id, "static_build", payload, app_id=app.id, timeout=BUILD_TIMEOUT_SECONDS)
        if done.status != "done":
            why = done.error_message or "unknown error"
            if "Unsupported command type" in why:
                why = f"node '{node.name}' runs an older Cloudbase; update it to build static sites"
            raise HTTPException(422, f"The build failed: {why}")
        if not _builds[build_id].received:
            raise HTTPException(500, f"Node '{node.name}' finished the build but sent no site")
    finally:
        _builds.pop(build_id, None)


async def _build_here(app: Application, push, archive: str) -> None:
    if not app.working_dir:
        raise HTTPException(400, "No source yet: deploy the site first")
    push("[Static] Building the site…")
    try:
        image, _built = await asyncio.to_thread(
            containers_build.build_image_unless_current,
            app.id, app.working_dir, lambda _aid, line: push(str(line)),
            app.app_type or "static", app.start_command or "", _image_port(app), **_image_build_kwargs(app),
        )
        await asyncio.to_thread(static_export.export_site, image, archive, max_bytes())
    except static_export.ExportFailed as e:
        raise HTTPException(422, f"The build failed: {e}") from None
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(422, f"The build failed: {e}") from None


# ── Publishing ───────────────────────────────────────────────────────────────

def _release_name(source_revision: Optional[str]) -> str:
    rev = re.sub(r"[^a-z0-9]", "", (source_revision or "").lower())[:12] or "site"
    return f"{rev}-{int(time.time())}-{secrets.token_hex(2)}"


def _ensure_site_dir(app_id: int) -> str:
    if not os.path.isdir(nginx_paths.SITES_DIR) or not os.access(nginx_paths.SITES_DIR, os.W_OK):
        raise HTTPException(500, f"{nginx_paths.SITES_DIR} is missing or not writable: update Cloudbase, or run "
                                 "'sudo bash scripts/setup-nginx-permissions.sh <user>' once")
    path = nginx_paths.site_dir(app_id)
    os.makedirs(path, exist_ok=True)
    os.chmod(path, 0o755)
    return path


def _prune(app_id: int, keep: set) -> None:
    """Remove all but the newest KEEP versions (and never one in `keep`), and what a failed publish left behind."""
    root = nginx_paths.site_dir(app_id)
    try:
        entries = [e for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return
    releases = sorted((e for e in entries if nginx_paths.RELEASE_RE.fullmatch(e.name)), key=lambda e: e.stat().st_mtime,
                      reverse=True)
    stale = [e.path for e in releases[KEEP:] if e.name not in keep]
    stale += [e.path for e in entries if e.name.endswith(".tmp") and time.time() - e.stat().st_mtime > 3600]
    for path in stale:
        shutil.rmtree(path, ignore_errors=True)


async def publish(app: Application, db: AsyncSession, push: Callable[[str], None], source_revision: Optional[str]) -> dict:
    """Make the site's files for the source as it is now and put them live. Raises HTTPException when that fails; the
    version that was live stays live."""
    root = await asyncio.to_thread(_ensure_site_dir, app.id)
    release = _release_name(source_revision)
    dest = nginx_paths.release_dir(app.id, release)
    tmp = dest + ".tmp"
    try:
        if (app.build_command or "").strip():
            archive = os.path.join(root, f".build-{secrets.token_hex(6)}.tar.gz")
            try:
                if tenant.enabled():
                    await _build_on_node(app, db, push, source_revision, archive)
                else:
                    await _build_here(app, push, archive)
                push("[Static] Unpacking the files…")
                count = await asyncio.to_thread(_unpack, archive, tmp)
            finally:
                if os.path.exists(archive):
                    os.remove(archive)
        else:
            push("[Static] No build command: publishing the files from the repository…")
            count = await asyncio.to_thread(_copy_from_source, app, tmp)
        if not os.path.isfile(os.path.join(tmp, "index.html")):
            push("[Static] Note: the site has no index.html at its top, so its address shows nothing")
        await asyncio.to_thread(os.rename, tmp, dest)
    finally:
        if os.path.exists(tmp):
            await asyncio.to_thread(shutil.rmtree, tmp, True)

    previous = app.static_release
    app.static_release, app.last_error = release, None
    await db.commit()
    try:
        if nginx_sync._has_public_nginx_domain(app):
            await nginx_sync._write_app_nginx_config(app, db)
    except Exception:
        app.static_release = previous                   # nginx did not take it: the old version stays live
        await db.commit()
        await asyncio.to_thread(shutil.rmtree, dest, True)
        raise
    push(f"[Static] Live: {count.files} files, {count.bytes / (1024 * 1024):.1f} MB, served by nginx")

    leftover = [i.id for i in await _load_app_instances(app.id, db)]
    if leftover:
        push("[Static] The site is served from disk now: its containers are removed")
        await remove_instances(app.id, leftover)
    await asyncio.to_thread(_prune, app.id, {release, previous})
    return {"release": release, "files": count.files, "size_bytes": count.bytes}


async def set_stopped(app: Application, db: AsyncSession, stopped: bool) -> None:
    """Stop (the downtime page) or start (the site) a site served from disk."""
    app.static_stopped = stopped
    await db.commit()
    if nginx_sync._has_public_nginx_domain(app):
        await nginx_sync._write_app_nginx_config(app, db)


def remove_site(app_id: int) -> None:
    """Every published version of the site (the app was deleted)."""
    shutil.rmtree(nginx_paths.site_dir(app_id), ignore_errors=True)
