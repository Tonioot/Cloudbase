from typing import Optional
from pydantic import BaseModel, Field

# ── Request models ────────────────────────────────────────────────────────────

class CreateDatabaseRequest(BaseModel):
    name: str
    project_id: Optional[int] = None
    engine: str
    version: Optional[str] = None
    node_id: Optional[int] = None
    cpu_limit: Optional[float] = None
    memory_limit_mb: Optional[int] = None
    # The size of the database's own disk, in MB: writes fail once it is full. Only at creation; it can grow later.
    disk_limit_mb: Optional[int] = None


class UpdateDatabaseRequest(BaseModel):
    cpu_limit: Optional[float] = None
    memory_limit_mb: Optional[int] = None
    disk_limit_mb: Optional[int] = None      # only bigger, and only for a database that was created with a limit
    clear_cpu_limit: bool = False
    clear_memory_limit: bool = False


class MoveDatabaseRequest(BaseModel):
    node_id: int


class PublicAccessRequest(BaseModel):
    enabled: bool
    allowlist: list[str] = Field(default_factory=list, max_length=100)
    tls: Optional[bool] = None  # None = keep the current setting
    hostname: Optional[str] = Field(default=None, max_length=253)  # None = keep; "" = back to the database domain / IP


class LinkRequest(BaseModel):
    app_id: int
    env_var_name: Optional[str] = Field(default=None, max_length=100)
    replace_env: Optional[bool] = None       # the app sets that name itself: let the link take its place
