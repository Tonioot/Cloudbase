"""The Cloudbase settings part of an export: what makes this installation yours apart from its apps and databases.

Users and their roles, notification channels and when they are sent, Git tokens, the backup storage, the base domain
for app subdomains, and the System settings (config.yaml). Secrets are in the clear (channel webhooks, tokens, the
storage key and the keys of encrypted backups), so only someone with `secrets.export` may export it, and only the
root account may import it: it can add accounts.

An import adds what this installation does not have yet: roles, users, channels and tokens whose name already exists
are skipped and reported. The base domain and the backup storage are only taken when none is set here. The
notification settings and the System settings are taken over, through the same checks as their pages.
"""
import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core import config as _cfg, system_config as _syscfg, token_vault
from core.env_crypto import decrypt_text, encrypt_text
from core.models import Application, NotificationChannel, Permission, Role, User, role_permissions
from core.system_settings import clean_system_settings
from dbs import offsite
from notify import channels as notify_channels, config as notify_config, settings as notify_settings

log = logging.getLogger("cloudbase.transfer")

BUILTIN_ROLES = ("Viewer", "Administrator")     # made at every start; never in a file


# ── Export ────────────────────────────────────────────────────────────────────

async def settings_entry(db: AsyncSession) -> dict:
    roles = []
    for role in (await db.execute(select(Role).where(Role.name.notin_(BUILTIN_ROLES)).order_by(Role.name))).scalars():
        perms = (await db.execute(
            select(Permission.name).join(role_permissions, Permission.id == role_permissions.c.permission_id)
            .where(role_permissions.c.role_id == role.id).order_by(Permission.name))).scalars().all()
        roles.append({"name": role.name, "description": role.description or "", "permissions": list(perms)})
    role_names = {r.id: r.name for r in (await db.execute(select(Role))).scalars()}
    users = [{"username": u.username, "password_hash": u.password_hash, "role": role_names.get(u.role_id)}
             for u in (await db.execute(select(User).where(User.username != "admin").order_by(User.username))).scalars()]

    app_names = {a.id: a.name for a in (await db.execute(select(Application))).scalars()}
    channels = []
    for c in (await db.execute(select(NotificationChannel).order_by(NotificationChannel.name))).scalars():
        apps = [app_names[i] for i in json.loads(c.app_ids or "[]") if i in app_names]
        channels.append({"name": c.name, "type": c.type, "config": notify_config.load_config(c), "enabled": bool(c.enabled),
                         "events": json.loads(c.events or "[]"), "apps": apps})

    storage = await offsite.load(db)
    if storage:
        storage = {**storage, "secret_key": decrypt_text(storage.get("secret_key") or ""),
                   "keys": offsite.keyring(storage)}

    return {
        "roles": roles,
        "users": users,
        "channels": channels,
        "notifications": await notify_settings.load_settings(db),
        "git_tokens": [{"label": t["label"], "token": decrypt_text(t["token"])} for t in token_vault.load()],
        "backup_storage": storage or None,
        "base_domain": _syscfg.get_base_domain_cached(),
        "system": {
            "auth": {"token_expire_seconds": _cfg.get_auth("token_expire_seconds")},
            "ports": {k: _cfg.get_port(k) for k in ("instance_min", "instance_max")},
            "limits": {k: _cfg.get_limit(k) for k in ("max_apps", "max_instances", "max_nodes",
                                                      "max_restarts_per_window", "restart_window_seconds")},
            "wireguard": {"endpoint": _cfg.get_wireguard().get("endpoint") or ""},
        },
    }


# ── Import ────────────────────────────────────────────────────────────────────

def _list(value) -> list:
    return value if isinstance(value, list) else []


def _text(value, limit: int = 200) -> str:
    return str(value or "").strip()[:limit]


async def import_settings(db: AsyncSession, entry: dict) -> dict:
    """Add what is missing here; returns {"added": [...], "skipped": [...]} as lines for the import report."""
    if not isinstance(entry, dict):
        raise ValueError("The settings in this file are not readable")
    added, skipped = [], []

    # Roles first: users refer to them by name
    perm_ids = {p.name: p.id for p in (await db.execute(select(Permission))).scalars()}
    roles = {r.name: r for r in (await db.execute(select(Role))).scalars()}
    for r in _list(entry.get("roles")):
        name = _text(r.get("name") if isinstance(r, dict) else "", 50)
        if not name:
            continue
        if name in roles:
            skipped.append(f"Role '{name}' (already exists)")
            continue
        role = Role(name=name, description=_text(r.get("description")))
        db.add(role)
        await db.flush()
        for perm in {p for p in _list(r.get("permissions")) if p in perm_ids}:
            await db.execute(role_permissions.insert().values(role_id=role.id, permission_id=perm_ids[perm]))
        roles[name] = role
        added.append(f"Role '{name}'")

    existing_users = set((await db.execute(select(User.username))).scalars())
    for u in _list(entry.get("users")):
        name = _text(u.get("username") if isinstance(u, dict) else "", 50)
        password_hash = _text(u.get("password_hash"), 300)
        if not name or not password_hash.startswith("$2"):                 # a bcrypt hash, nothing else
            continue
        if name in existing_users or name == "admin":
            skipped.append(f"User '{name}' (already exists)")
            continue
        role = roles.get(_text(u.get("role"), 50)) or roles.get("Viewer")
        db.add(User(username=name, password_hash=password_hash, role=role.name if role else "Viewer",
                    role_id=role.id if role else None))
        added.append(f"User '{name}' ({role.name if role else 'no role'}), with the password it had")

    app_ids = {a.name: a.id for a in (await db.execute(select(Application))).scalars()}
    channel_names = set((await db.execute(select(NotificationChannel.name))).scalars())
    for c in _list(entry.get("channels")):
        name = _text(c.get("name") if isinstance(c, dict) else "", 100)
        if not name or c.get("type") not in notify_channels.CHANNEL_TYPES or not isinstance(c.get("config"), dict):
            continue
        if name in channel_names:
            skipped.append(f"Notification channel '{name}' (already exists)")
            continue
        channel = NotificationChannel(name=name, type=c["type"], enabled=bool(c.get("enabled", True)),
                                      events=json.dumps([e for e in _list(c.get("events")) if isinstance(e, str)]),
                                      app_ids=json.dumps([app_ids[a] for a in _list(c.get("apps")) if a in app_ids]))
        notify_config.store_config(channel, c["config"])
        db.add(channel)
        added.append(f"Notification channel '{name}'")

    if isinstance(entry.get("notifications"), dict):
        await notify_settings.save_settings(db, entry["notifications"])
        added.append("Notification settings")

    labels = {t["label"] for t in token_vault.load()}
    for t in _list(entry.get("git_tokens")):
        label, token = _text(t.get("label") if isinstance(t, dict) else ""), _text(t.get("token"), 1000)
        if not label or not token:
            continue
        if label in labels:
            skipped.append(f"Git token '{label}' (already exists)")
            continue
        token_vault.add(label, token)
        added.append(f"Git token '{label}'")

    storage = entry.get("backup_storage")
    if isinstance(storage, dict) and storage.get("bucket"):
        current = await offsite.load(db)
        if offsite.is_active(current):
            skipped.append("Backup storage (this server has its own)")
        else:
            keys = {str(k): encrypt_text(str(v)) for k, v in (storage.get("keys") or {}).items() if v}
            await offsite.save(db, {**{k: storage.get(k) for k in offsite.FIELDS},
                                    "secret_key": encrypt_text(_text(storage.get("secret_key"), 500)),
                                    "keys": keys, "current_key_id": storage.get("current_key_id")})
            added.append("Backup storage" + (" with the keys of its encrypted backups" if keys else ""))

    domain = _text(entry.get("base_domain"), 253)
    if domain:
        if _syscfg.get_base_domain_cached():
            skipped.append("Base domain (this server has its own)")
        else:
            await _syscfg.set_base_settings(db, base_domain=domain, base_ssl_cert="", base_ssl_key="")
            added.append(f"Base domain {domain}")

    if isinstance(entry.get("system"), dict):
        sections = clean_system_settings(entry["system"])
        if sections:
            _cfg.save_config_sections(sections)
            added.append("System settings")
    return {"added": added, "skipped": skipped}


async def preview_settings(db: AsyncSession, entry: dict) -> list[dict]:
    """What import_settings would do, without doing it: [{what, name, action: add | skip | replace, reason}]."""
    if not isinstance(entry, dict):
        return []
    out = []
    roles = set((await db.execute(select(Role.name))).scalars())
    users = set((await db.execute(select(User.username))).scalars())
    channels = set((await db.execute(select(NotificationChannel.name))).scalars())
    tokens = {t["label"] for t in token_vault.load()}
    for what, key, field, existing in (("Role", "roles", "name", roles), ("User", "users", "username", users),
                                       ("Notification channel", "channels", "name", channels),
                                       ("Git token", "git_tokens", "label", tokens)):
        for item in _list(entry.get(key)):
            name = _text(item.get(field) if isinstance(item, dict) else "", 100)
            if name:
                out.append({"what": what, "name": name, "action": "skip" if name in existing or (key == "users" and name == "admin") else "add",
                            "reason": "already exists" if name in existing else ""})
    if isinstance(entry.get("notifications"), dict):
        out.append({"what": "Notification settings", "name": "", "action": "replace", "reason": ""})
    storage = entry.get("backup_storage")
    if isinstance(storage, dict) and storage.get("bucket"):
        mine = offsite.is_active(await offsite.load(db))
        out.append({"what": "Backup storage", "name": str(storage.get("bucket")), "action": "skip" if mine else "add",
                    "reason": "this server has its own" if mine else ""})
    domain = _text(entry.get("base_domain"), 253)
    if domain:
        mine = bool(_syscfg.get_base_domain_cached())
        out.append({"what": "Base domain", "name": domain, "action": "skip" if mine else "add",
                    "reason": "this server has its own" if mine else ""})
    if isinstance(entry.get("system"), dict):
        out.append({"what": "System settings", "name": "", "action": "replace", "reason": ""})
    return out
