from datetime import datetime, timezone
from enum import Enum
from pydantic import BaseModel, Field
from typing import Optional


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class WorkerStatus(str, Enum):
    online = "online"
    offline = "offline"


class JobStatus(str, Enum):
    starting = "starting"
    running = "running"
    stopped = "stopped"
    failed = "failed"


class WorkerRegisterRequest(BaseModel):
    worker_id: str
    address: str
    port: int = 9000
    capacity: int = Field(default=1, ge=1)


class WorkerHeartbeatRequest(BaseModel):
    worker_id: str
    active_jobs: int = Field(default=0, ge=0)
    capacity: int = Field(default=1, ge=1)
    status: WorkerStatus = WorkerStatus.online


class WorkerInfo(BaseModel):
    worker_id: str
    address: str
    port: int = 9000
    capacity: int = 1
    active_jobs: int = 0
    status: WorkerStatus = WorkerStatus.online
    last_heartbeat: datetime = Field(default_factory=now_utc)

    @property
    def api_base_url(self) -> str:
        return f"http://{self.address}:{self.port}"

    def public_dict(self) -> dict:
        data = self.model_dump(mode="json")
        data["api_base_url"] = self.api_base_url
        data["free_capacity"] = max(self.capacity - self.active_jobs, 0)
        return data


class JobInfo(BaseModel):
    job_id: str
    status: JobStatus
    worker_id: str
    protocol: str = "tcp"
    gateway_port: int
    internal_host: str
    internal_port: int
    container_id: Optional[str] = None
    container_name: Optional[str] = None
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)
    error: Optional[str] = None

    def public_dict(self) -> dict:
        return self.model_dump(mode="json")


class CreateJobResponse(BaseModel):
    job_id: str
    status: JobStatus
    worker_id: str
    port: int
    protocol: str = "tcp"
    connect: str


class SystemStatus(BaseModel):
    gateway: str = "online"
    workers_online: int
    workers_total: int
    active_jobs: int
    total_capacity: int
    free_capacity: int
    open_ports: int
