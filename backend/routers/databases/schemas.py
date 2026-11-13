from typing import Optional
from pydantic import BaseModel, Field

# ── Request models ────────────────────────────────────────────────────────────

class CreateDatabaseRequest(BaseModel):
    name: str
    engine: str
    version: Optional[str] = None
    node_id: Optional[int] = None
    cpu_limit: Optional[float] = None
    memory_limit_mb: Optional[int] = None


class UpdateDatabaseRequest(BaseModel):
    cpu_limit: Optional[float] = None
    memory_limit_mb: Optional[int] = None
    clear_cpu_limit: bool = False
    clear_memory_limit: bool = False


class PublicAccessRequest(BaseModel):
    enabled: bool
    allowlist: list[str] = Field(default_factory=list, max_length=100)
    tls: Optional[bool] = None  # None = keep the current setting


class LinkRequest(BaseModel):
    app_id: int
    env_var_name: Optional[str] = Field(default=None, max_length=100)
    replace_env: Optional[bool] = None       # the app sets that name itself: let the link take its place
