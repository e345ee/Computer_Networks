import logging
import os
import re
from datetime import datetime, timezone

import docker
from docker.errors import APIError, DockerException, NotFound

from .models import JobStatus, WorkerJob

logger = logging.getLogger(__name__)


def safe_name(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value)
    return value[:120].strip("-") or "job"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DockerRuntime:
    def __init__(self) -> None:
        self.worker_id = os.getenv("WORKER_ID", "worker-1")
        self.image = os.getenv("JOB_IMAGE", "distributed-job-runner:latest")
        self.network = os.getenv("DOCKER_NETWORK", "distributed-compute-net")
        self.job_port = int(os.getenv("JOB_INTERNAL_PORT", "7001"))
        self.client = docker.from_env()

    def create_job(self, job_id: str) -> WorkerJob:
        container_name = safe_name(f"dc-{self.worker_id}-{job_id}")
        logger.info("starting job container job_id=%s name=%s image=%s", job_id, container_name, self.image)
        try:
            try:
                old = self.client.containers.get(container_name)
                old.remove(force=True)
            except NotFound:
                pass

            container = self.client.containers.run(
                self.image,
                detach=True,
                name=container_name,
                network=self.network,
                environment={
                    "JOB_ID": job_id,
                    "JOB_PORT": str(self.job_port),
                },
                labels={
                    "distributed-computing-platform.job": "true",
                    "distributed-computing-platform.worker_id": self.worker_id,
                    "distributed-computing-platform.job_id": job_id,
                },
            )
            return WorkerJob(
                job_id=job_id,
                status=JobStatus.running,
                internal_host=container_name,
                internal_port=self.job_port,
                container_id=container.id,
                container_name=container_name,
            )
        except (DockerException, APIError) as exc:
            logger.exception("docker failed to start job job_id=%s", job_id)
            raise RuntimeError(f"failed to start job container: {exc}") from exc


    def recover_jobs(self) -> dict[str, WorkerJob]:
        """Recover job containers that are still present after worker/gateway restart."""
        recovered: dict[str, WorkerJob] = {}
        try:
            containers = self.client.containers.list(
                all=True,
                filters={
                    "label": [
                        "distributed-computing-platform.job=true",
                        f"distributed-computing-platform.worker_id={self.worker_id}",
                    ]
                },
            )
        except (DockerException, APIError) as exc:
            logger.exception("docker failed to list recoverable jobs for worker=%s", self.worker_id)
            raise RuntimeError(f"failed to recover job containers: {exc}") from exc

        for container in containers:
            labels = container.labels or {}
            job_id = labels.get("distributed-computing-platform.job_id")
            if not job_id:
                continue
            try:
                container.reload()
                status = JobStatus.running if container.status == "running" else JobStatus.stopped
                recovered[job_id] = WorkerJob(
                    job_id=job_id,
                    status=status,
                    internal_host=container.name,
                    internal_port=self.job_port,
                    container_id=container.id,
                    container_name=container.name,
                )
            except (DockerException, APIError) as exc:
                logger.warning("failed to recover job container=%s err=%s", container.name, exc)
        logger.info("recovered %s job containers for worker=%s", len(recovered), self.worker_id)
        return recovered

    def delete_job(self, job: WorkerJob) -> WorkerJob:
        logger.info("deleting job container job_id=%s name=%s", job.job_id, job.container_name)
        try:
            if job.container_id:
                container = self.client.containers.get(job.container_id)
            elif job.container_name:
                container = self.client.containers.get(job.container_name)
            else:
                raise NotFound("container id/name missing")
            container.remove(force=True)
            job.status = JobStatus.stopped
            job.updated_at = utcnow()
            return job
        except NotFound:
            job.status = JobStatus.stopped
            job.updated_at = utcnow()
            return job
        except (DockerException, APIError) as exc:
            job.status = JobStatus.failed
            job.error = str(exc)
            job.updated_at = utcnow()
            logger.exception("docker failed to delete job job_id=%s", job.job_id)
            raise RuntimeError(f"failed to delete job container: {exc}") from exc

    def refresh_status(self, job: WorkerJob) -> WorkerJob:
        try:
            if not job.container_id:
                return job
            container = self.client.containers.get(job.container_id)
            container.reload()
            if container.status == "running":
                job.status = JobStatus.running
            elif container.status in {"exited", "dead", "removing"}:
                job.status = JobStatus.stopped
            else:
                job.status = JobStatus.running
            job.updated_at = utcnow()
            return job
        except NotFound:
            job.status = JobStatus.stopped
            job.updated_at = utcnow()
            return job
