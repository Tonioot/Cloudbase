from pydantic import Field, BaseModel

# ── Endpoints ─────────────────────────────────────────────────────────────────

class BackupSettingsRequest(BaseModel):
    schedule: str
    hour: int = Field(default=3, ge=0, le=23)
    retention: int = Field(default=7, ge=1, le=365)


class RestoreRequest(BaseModel):
    backup_id: int
    safety_backup: bool = True


# ── Off-site storage settings ─────────────────────────────────────────────────

class StorageSettingsRequest(BaseModel):
    enabled: bool = False
    encrypt: bool = False
    endpoint: str = ""
    region: str = ""
    bucket: str = ""
    prefix: str = "cloudbase/"
    access_key: str = ""
    secret_key: str = ""  # empty = keep the stored secret


class KeyRequest(BaseModel):
    key: str
    make_current: bool = False
