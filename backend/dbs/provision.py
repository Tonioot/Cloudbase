import re
import secrets
from typing import Optional
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from dbs import engines as db_engines, links as db_links
from core.models import SystemConfig, Database

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")


ENV_VAR_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


MIN_MEMORY_MB = {"redis": 32, "valkey": 32, "postgres": 128}


DEFAULT_MIN_MEMORY_MB = 256


# ── Helpers ───────────────────────────────────────────────────────────────────

def _spec(database: Database) -> dict:
    return {
        "database_id": database.id,
        "name": database.name,
        "engine": database.engine,
        "version": database.version,
        "credentials": db_links.decrypt_credentials(database),
        "cpu_limit": database.cpu_limit,
        "memory_limit_mb": database.memory_limit_mb,
        "initialized": database.initialized_at is not None,
        "volume_token": _volume_token(database),
    }


def _volume_token(database: Database) -> str:
    """Identifies the data volume of exactly this database (see db_manager.ensure_running)."""
    created = database.created_at.isoformat() if database.created_at else ""
    return f"{database.id}:{created}"


async def _next_database_id(db: AsyncSession) -> int:
    """Database ids are never reused: they name the data volume and the ports, and a
    new database must not pick up the volume of a deleted one. SQLite reuses the
    highest id after a delete, so keep our own high-water mark."""
    row = (await db.execute(select(SystemConfig).where(SystemConfig.key == "databases.last_id"))).scalar_one_or_none()
    highest = (await db.execute(select(func.max(Database.id)))).scalar() or 0
    next_id = max(int(row.value or 0) if row else 0, highest) + 1
    if row is None:
        db.add(SystemConfig(key="databases.last_id", value=str(next_id)))
    else:
        row.value = str(next_id)
    return next_id


def _data_dir(database: Database) -> str:
    return db_engines.get_engine(database.engine).data_dir(database.version)


def _new_credentials(name: str, engine: str) -> dict:
    ident = name.replace("-", "_")
    return {
        "user": "default" if engine in ("redis", "valkey") else ident,
        "password": secrets.token_urlsafe(24),
        "root_password": secrets.token_urlsafe(24),
        "db_name": ident,
    }


def _validate_limits(engine: str, cpu_limit: Optional[float], memory_limit_mb: Optional[int]) -> None:
    if cpu_limit is not None and not (0.1 <= cpu_limit <= 64):
        raise HTTPException(400, "CPU limit must be between 0.1 and 64 cores")
    if memory_limit_mb is not None:
        minimum = MIN_MEMORY_MB.get(engine, DEFAULT_MIN_MEMORY_MB)
        if memory_limit_mb < minimum:
            raise HTTPException(400, f"{db_engines.get_engine(engine).label} needs at least {minimum} MB of memory")
