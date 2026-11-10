from typing import Optional
from pydantic import Field, BaseModel

class InviteRequest(BaseModel):
    note: Optional[str] = None
    ttl_minutes: int = Field(default=30, ge=1, le=1440)


class RegisterNodeRequest(BaseModel):
    invite_code: str
    name: str
    public_host: Optional[str] = None
    capabilities: Optional[dict] = None
    metadata_json: Optional[dict] = None
    heartbeat_interval: int = Field(default=15, ge=5, le=300)


class ReplicaReport(BaseModel):
    state: str = Field(max_length=20)
    substatus: Optional[str] = Field(default=None, max_length=40)
    container_id: Optional[str] = Field(default=None, max_length=200)
    spec_hash: Optional[str] = Field(default=None, max_length=20)
    error: Optional[str] = Field(default=None, max_length=2000)


class ObservedRequest(BaseModel):
    replicas: dict[str, ReplicaReport] = Field(default_factory=dict, max_length=5000)


class WireguardEnrollRequest(BaseModel):
    public_key: str = Field(min_length=44, max_length=44)


class CommandResultRequest(BaseModel):
    status: str
    result: Optional[dict] = None
    error_message: Optional[str] = None


class NodeUpdateRequest(BaseModel):
    name: Optional[str] = None
