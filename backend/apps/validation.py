import asyncio
import json
import re
from typing import Optional
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from core.models import Application, ApplicationReplica
from nginx import paths as nginx_paths
from containers import apps as containers_apps

async def _assign_external_port(requested: Optional[int], node_id: int, db: AsyncSession) -> int:
    """Return requested port if free, else the next free port of the instance range, for an instance on this node."""
    replica_result = await db.execute(
        select(ApplicationReplica.external_port).where(
            ApplicationReplica.node_id == node_id,
            ApplicationReplica.external_port.isnot(None),
        )
    )
    used: set[int] = {row[0] for row in replica_result.all()}

    if requested:
        if requested in used:
            raise HTTPException(400, f"External port {requested} is already used by another app on this node")
        return requested

    return await asyncio.to_thread(containers_apps.pick_free_external_port, used)


async def _check_domain_conflicts(
    domains: list[str],
    db: AsyncSession,
    exclude_app_id: Optional[int] = None,
) -> None:
    """Raise 400 if any domain in *domains* is already used by another app."""
    clean = [d.strip().lower() for d in domains if d and d.strip()]
    if not clean:
        return
    result = await db.execute(
        select(Application.id, Application.name, Application.domain,
               Application.extra_domains, Application.redirect_domains).where(
            Application.id != exclude_app_id if exclude_app_id else True,
        )
    )
    for row in result.all():
        other_id, other_name, primary, extra_raw, redirect_raw = row
        other_domains = set()
        if primary:
            other_domains.add(primary.strip().lower())
        for lst_raw in (extra_raw, redirect_raw):
            try:
                for d in json.loads(lst_raw or "[]"):
                    if d:
                        other_domains.add(d.strip().lower())
            except Exception:
                pass
        clash = set(clean) & other_domains
        if clash:
            raise HTTPException(
                400,
                f"Domain '{next(iter(clash))}' is already used by app '{other_name}'",
            )


def _validate_docker_runtime_settings(
    cpu_limit: Optional[float],
    memory_limit_mb: Optional[int],
    tmpfs_size_mb: Optional[int],
) -> None:
    if cpu_limit is not None and cpu_limit <= 0:
        raise HTTPException(400, "docker_cpu_limit must be greater than 0")
    if memory_limit_mb is not None and memory_limit_mb <= 0:
        raise HTTPException(400, "docker_memory_limit_mb must be greater than 0")
    if tmpfs_size_mb is not None and tmpfs_size_mb <= 0:
        raise HTTPException(400, "docker_tmpfs_size_mb must be greater than 0")


# names the panel itself uses (its own nginx site and automatic subdomain)
_RESERVED_APP_NAMES = {"cloudbase", "cloudbase-default", "zz-cloudbase-acme", "default"}


_REPO_URL_RE = re.compile(r"^https?://[^\s]+$")


# Commits, branches and other refs passed to git: never an option ("-..."), no whitespace
_GIT_REF_RE = re.compile(r"^[A-Za-z0-9._/~^@{}-]{1,200}$")


async def _check_new_app_name(name: Optional[str], db: AsyncSession, exclude_id: Optional[int] = None) -> str:
    name = (name or "").strip()
    if not name or len(name) > 100:
        raise HTTPException(400, "App name must be 1–100 characters")
    safe = nginx_paths._safe_name(name)
    if safe in _RESERVED_APP_NAMES:
        raise HTTPException(400, f"The name '{name}' is reserved by Cloudbase")
    # Two apps whose names give the same automatic subdomain would answer on the same address
    slug = nginx_paths.subdomain_slug(name)
    for other_id, other in (await db.execute(select(Application.id, Application.name))).all():
        if other_id == exclude_id:
            continue
        if other == name or (slug and nginx_paths.subdomain_slug(other) == slug):
            raise HTTPException(400, f"App '{other}' already uses this name")
    return name


def _check_repo_url(url: Optional[str]) -> str:
    url = (url or "").strip()
    if not _REPO_URL_RE.match(url):
        raise HTTPException(400, "Repository URL must start with https:// or http:// (SSH links are not supported)")
    return url


def _check_git_ref(ref: Optional[str]) -> Optional[str]:
    ref = (ref or "").strip() or None
    if ref is not None and (ref.startswith("-") or ".." in ref or not _GIT_REF_RE.match(ref)):
        raise HTTPException(400, "Invalid commit or branch")
    return ref
