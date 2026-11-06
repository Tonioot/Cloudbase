from typing import Optional
from pydantic import Field, BaseModel

class InviteRequest(BaseModel):
    note: Optional[str] = None
    ttl_minutes: int = Field(default=30, ge=1, le=1440)


class RegisterNodeRequest(BaseModel):
    invite_code: str
    name: str
    role: str = "node"
    api_base_url: Optional[str] = None
    public_host: Optional[str] = None
    capabilities: Optional[dict] = None
    metadata_json: Optional[dict] = None
    heartbeat_interval: int = Field(default=15, ge=5, le=300)


class WireguardEnrollRequest(BaseModel):
    public_key: str = Field(min_length=44, max_length=44)


class CommandResultRequest(BaseModel):
    status: str
    result: Optional[dict] = None
    error_message: Optional[str] = None


class NodeUpdateRequest(BaseModel):
    name: Optional[str] = None
