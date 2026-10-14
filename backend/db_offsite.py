"""Off-site copies of database backups in S3-compatible storage (primary only).

Works with AWS S3, Cloudflare R2, Backblaze B2, MinIO and anything else that
speaks the S3 API. Settings live in system_config under "backups.s3"; the secret
key is stored encrypted.
"""

import json
import logging
import os
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from env_crypto import decrypt_text, encrypt_text
from models import SystemConfig

log = logging.getLogger("cloudbase.backups")

CONFIG_KEY = "backups.s3"
FIELDS = ("enabled", "endpoint", "region", "bucket", "prefix", "access_key")


async def load(db: AsyncSession) -> dict:
    row = (await db.execute(select(SystemConfig).where(SystemConfig.key == CONFIG_KEY))).scalar_one_or_none()
    try:
        return json.loads(row.value) if row and row.value else {}
    except ValueError:
        return {}


async def save(db: AsyncSession, settings: dict) -> None:
    row = (await db.execute(select(SystemConfig).where(SystemConfig.key == CONFIG_KEY))).scalar_one_or_none()
    if row is None:
        db.add(SystemConfig(key=CONFIG_KEY, value=json.dumps(settings)))
    else:
        row.value = json.dumps(settings)


def public_view(settings: dict) -> dict:
    """Settings for the panel: everything except the secret itself."""
    view = {k: settings.get(k) for k in FIELDS}
    view["enabled"] = bool(settings.get("enabled"))
    view["prefix"] = settings.get("prefix") or "cloudbase/"
    view["secret_set"] = bool(settings.get("secret_key"))
    return view


def is_active(settings: dict) -> bool:
    return bool(settings.get("enabled") and settings.get("bucket") and settings.get("access_key")
                and settings.get("secret_key"))


def _client(settings: dict):
    import boto3
    from botocore.config import Config
    endpoint = (settings.get("endpoint") or "").strip() or None
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=(settings.get("region") or "").strip() or ("auto" if endpoint else "us-east-1"),
        aws_access_key_id=settings.get("access_key"),
        aws_secret_access_key=decrypt_text(settings.get("secret_key") or ""),
        config=Config(
            signature_version="s3v4",
            # Path-style works for every S3-compatible service; AWS itself prefers virtual hosts.
            s3={"addressing_style": "path" if endpoint else "auto"},
            retries={"max_attempts": 5, "mode": "standard"},
            connect_timeout=15,
            read_timeout=120,
        ),
    )


def object_key(settings: dict, file_name: str) -> str:
    prefix = (settings.get("prefix") or "cloudbase/").strip().lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return f"{prefix}database-backups/{file_name}"


# Blocking calls (run them in a thread).

def upload(settings: dict, path: str, key: str) -> None:
    _client(settings).upload_file(path, settings["bucket"], key)  # multipart for large files


def download(settings: dict, key: str, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    part = path + ".part"
    _client(settings).download_file(settings["bucket"], key, part)
    os.replace(part, path)
    os.chmod(path, 0o600)


def delete(settings: dict, key: str) -> None:
    _client(settings).delete_object(Bucket=settings["bucket"], Key=key)


def test(settings: dict) -> Optional[str]:
    """Write, read back and delete a small object. Returns an error message or None."""
    key = object_key(settings, ".cloudbase-connection-test")
    try:
        client = _client(settings)
        client.put_object(Bucket=settings["bucket"], Key=key, Body=b"cloudbase")
        body = client.get_object(Bucket=settings["bucket"], Key=key)["Body"].read()
        client.delete_object(Bucket=settings["bucket"], Key=key)
        return None if body == b"cloudbase" else "The object read back differs from what was written"
    except Exception as e:  # botocore errors carry a readable message
        return str(e)


def merge_update(current: dict, update: dict) -> dict:
    """Apply panel input; an empty secret keeps the stored one."""
    merged = dict(current)
    for k in FIELDS:
        if k in update:
            merged[k] = update[k].strip() if isinstance(update[k], str) else update[k]
    if update.get("secret_key"):
        merged["secret_key"] = encrypt_text(update["secret_key"].strip())
    return merged
