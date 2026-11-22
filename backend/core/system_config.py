from __future__ import annotations

from typing import Dict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import SystemConfig
from core import config as _cfg

_BASE_DOMAIN_KEY = "base_domain"
_BASE_SSL_CERT_KEY = "base_ssl_cert"
_BASE_SSL_KEY_KEY = "base_ssl_key"
_DB_DOMAIN_KEY = "db_domain"
_DB_SSL_CERT_KEY = "db_ssl_cert"
_DB_SSL_KEY_KEY = "db_ssl_key"

_cache: Dict[str, str] = {}

# The panel's domain when it is served over HTTPS (read from its nginx config), else None
_panel_https_domain: str | None = None


def set_panel_https_domain(domain: str | None) -> None:
    global _panel_https_domain
    _panel_https_domain = domain or None


def get_panel_https_domain() -> str | None:
    return _panel_https_domain


def _normalize(value: str | None) -> str:
    return str(value or "").strip()


def get_base_domain_cached() -> str:
    value = _normalize(_cache.get(_BASE_DOMAIN_KEY))
    if value:
        return value
    return _cfg.get_base_domain()


def get_db_domain_cached() -> str:
    """The domain databases are reached under from outside (<name>.<db domain>); empty = the primary's IP."""
    return _normalize(_cache.get(_DB_DOMAIN_KEY))


def get_db_ssl_cached() -> tuple[str, str]:
    """(certificate path, key path) of the wildcard certificate uploaded for the database domain, or ("", "")."""
    return _normalize(_cache.get(_DB_SSL_CERT_KEY)), _normalize(_cache.get(_DB_SSL_KEY_KEY))


async def set_db_domain(db: AsyncSession, domain: str, ssl_cert: str = "", ssl_key: str = "") -> None:
    values = {_DB_DOMAIN_KEY: _normalize(domain), _DB_SSL_CERT_KEY: _normalize(ssl_cert), _DB_SSL_KEY_KEY: _normalize(ssl_key)}
    rows = {r.key: r for r in (await db.execute(select(SystemConfig).where(SystemConfig.key.in_(list(values))))).scalars()}
    for key, value in values.items():
        if key in rows:
            rows[key].value = value
        else:
            db.add(SystemConfig(key=key, value=value))
        _cache[key] = value
    await db.commit()


def get_base_ssl_cert_cached() -> str:
    value = _normalize(_cache.get(_BASE_SSL_CERT_KEY))
    if value:
        return value
    return _cfg.get_base_ssl_cert()


def get_base_ssl_key_cached() -> str:
    value = _normalize(_cache.get(_BASE_SSL_KEY_KEY))
    if value:
        return value
    return _cfg.get_base_ssl_key()


async def load_cache(db: AsyncSession) -> None:
    result = await db.execute(
        select(SystemConfig).where(
            SystemConfig.key.in_([
                _BASE_DOMAIN_KEY,
                _BASE_SSL_CERT_KEY,
                _BASE_SSL_KEY_KEY,
                _DB_DOMAIN_KEY,
                _DB_SSL_CERT_KEY,
                _DB_SSL_KEY_KEY,
            ])
        )
    )
    rows = result.scalars().all()
    for row in rows:
        _cache[row.key] = _normalize(row.value)


async def bootstrap_from_config_if_needed(db: AsyncSession) -> None:
    await load_cache(db)
    values = {
        _BASE_DOMAIN_KEY: _cfg.get_base_domain(),
        _BASE_SSL_CERT_KEY: _cfg.get_base_ssl_cert(),
        _BASE_SSL_KEY_KEY: _cfg.get_base_ssl_key(),
    }
    changed = False
    for key, value in values.items():
        if key not in _cache:
            db.add(SystemConfig(key=key, value=_normalize(value)))
            _cache[key] = _normalize(value)
            changed = True
    if changed:
        await db.commit()


async def set_base_settings(
    db: AsyncSession,
    *,
    base_domain: str,
    base_ssl_cert: str,
    base_ssl_key: str,
) -> None:
    values = {
        _BASE_DOMAIN_KEY: _normalize(base_domain),
        _BASE_SSL_CERT_KEY: _normalize(base_ssl_cert),
        _BASE_SSL_KEY_KEY: _normalize(base_ssl_key),
    }

    result = await db.execute(
        select(SystemConfig).where(
            SystemConfig.key.in_(list(values.keys()))
        )
    )
    existing = {row.key: row for row in result.scalars().all()}

    for key, value in values.items():
        if key in existing:
            existing[key].value = value
        else:
            db.add(SystemConfig(key=key, value=value))
        _cache[key] = value

    await db.commit()


async def get_base_settings(db: AsyncSession) -> dict:
    await load_cache(db)
    return {
        "base_domain": get_base_domain_cached(),
        "base_ssl_cert_path": get_base_ssl_cert_cached(),
        "base_ssl_key_path": get_base_ssl_key_cached(),
        "db_domain": get_db_domain_cached(),
        "db_ssl_cert_path": get_db_ssl_cached()[0],
    }
