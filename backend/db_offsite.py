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
FIELDS = ("enabled", "endpoint", "region", "bucket", "prefix", "access_key", "encrypt")


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
    view["encrypt"] = bool(settings.get("encrypt"))
    view["current_key_id"] = settings.get("current_key_id")
    view["key_ids"] = sorted((settings.get("keys") or {}).keys())
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


# ── Encryption of off-site copies ─────────────────────────────────────────────
# Format: b"CBENC1" | key id (8 ASCII hex) | nonce prefix (8 bytes) |
#         chunks of [4-byte length | AES-256-GCM ciphertext+tag].
# Chunk nonce = prefix + 4-byte counter; the associated data marks the last chunk,
# so a truncated file is detected, not silently accepted.

MAGIC = b"CBENC1"
CHUNK = 1024 * 1024
KEY_PREFIX = "cbk1_"
ENCRYPTED_SUFFIX = ".cbenc"


def new_key() -> str:
    import base64
    return KEY_PREFIX + base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")


def _key_bytes(key: str) -> bytes:
    import base64
    if not key.startswith(KEY_PREFIX):
        raise ValueError("Not a Cloudbase backup key (it starts with cbk1_)")
    raw = key[len(KEY_PREFIX):]
    data = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    if len(data) != 32:
        raise ValueError("The key has the wrong length")
    return data


def key_id(key: str) -> str:
    import hashlib
    return hashlib.sha256(_key_bytes(key)).hexdigest()[:8]


def encrypt_file(src: str, dst: str, key: str) -> None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    aes, kid, prefix = AESGCM(_key_bytes(key)), key_id(key).encode(), os.urandom(8)
    with open(src, "rb") as f, open(dst, "wb") as out:
        out.write(MAGIC + kid + prefix)
        counter = 0
        chunk = f.read(CHUNK)
        while True:
            following = f.read(CHUNK)
            last = not following
            nonce = prefix + counter.to_bytes(4, "big")
            sealed = aes.encrypt(nonce, chunk, b"last" if last else b"more")
            out.write(len(sealed).to_bytes(4, "big") + sealed)
            if last:
                break
            chunk, counter = following, counter + 1


def encrypted_key_id(path: str) -> Optional[str]:
    with open(path, "rb") as f:
        head = f.read(len(MAGIC) + 8)
    return head[len(MAGIC):].decode() if head.startswith(MAGIC) else None


def decrypt_file(src: str, dst: str, keys: dict[str, str]) -> None:
    """keys: key id → key. Raises when no key fits or the file was altered/truncated.
    `dst` only appears when everything decrypted correctly."""
    part = dst + ".part"
    try:
        _decrypt_to(src, part, keys)
        os.replace(part, dst)
    finally:
        if os.path.exists(part):
            os.remove(part)


def _decrypt_to(src: str, dst: str, keys: dict[str, str]) -> None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    with open(src, "rb") as f, open(dst, "wb") as out:
        head = f.read(len(MAGIC) + 16)
        if not head.startswith(MAGIC):
            raise ValueError("Not an encrypted Cloudbase backup")
        kid, prefix = head[len(MAGIC):len(MAGIC) + 8].decode(), head[len(MAGIC) + 8:]
        if kid not in keys:
            raise ValueError(f"This backup was encrypted with key {kid}, which this server does not have. "
                             "Add that key under Settings → Backup storage.")
        aes, counter, finished = AESGCM(_key_bytes(keys[kid])), 0, False
        while True:
            size = f.read(4)
            if not size:
                break
            if finished:
                raise ValueError("Unexpected data after the last block")
            sealed = f.read(int.from_bytes(size, "big"))
            nonce = prefix + counter.to_bytes(4, "big")
            try:
                out.write(aes.decrypt(nonce, sealed, b"last"))
                finished = True
            except Exception:
                out.write(aes.decrypt(nonce, sealed, b"more"))  # raises if altered
            counter += 1
        if not finished:
            raise ValueError("The backup is incomplete (truncated)")


def keyring(settings: dict) -> dict[str, str]:
    """All known keys, decrypted: key id → key."""
    return {kid: decrypt_text(enc) for kid, enc in (settings.get("keys") or {}).items()}


def current_key(settings: dict) -> Optional[str]:
    kid = settings.get("current_key_id")
    return keyring(settings).get(kid) if kid else None


def add_key(settings: dict, key: str, make_current: bool) -> dict:
    kid = key_id(key)
    merged = dict(settings)
    merged["keys"] = {**(settings.get("keys") or {}), kid: encrypt_text(key)}
    if make_current or not merged.get("current_key_id"):
        merged["current_key_id"] = kid
    return merged
