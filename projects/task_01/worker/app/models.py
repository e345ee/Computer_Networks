from datetime import datetime, timezone
from enum import Enum
from pydantic import BaseModel, Field
from typing import Optional


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus(str, Enum):
    running = "running"
    stopped = "stopped"
    failed = "failed"


class CreateInternalJobRequest(BaseModel):
    job_id: str


class WorkerJob(BaseModel):
    job_id: str
    status: JobStatus
    internal_host: str
    internal_port: int
    container_id: Optional[str] = None
    container_name: Optional[str] = None
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)
    error: Optional[str] = None

    def public_dict(self) -> dict:
        return self.model_dump(mode="json")
