import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import JobInfo, JobStatus, WorkerInfo, WorkerStatus

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Registry:
    def __init__(
        self,
        port_start: int,
        port_end: int,
        worker_stale_seconds: int,
        state_path: str | None = None,
    ) -> None:
        self.workers: dict[str, WorkerInfo] = {}
        self.jobs: dict[str, JobInfo] = {}
        self.used_ports: set[int] = set()
        self.port_start = port_start
        self.port_end = port_end
        self.worker_stale_seconds = worker_stale_seconds
        self.state_path = Path(state_path) if state_path else None
        self._lock = asyncio.Lock()
        self._load_state()

    def _load_state(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.workers = {
                item["worker_id"]: WorkerInfo.model_validate(item)
                for item in raw.get("workers", [])
                if item.get("worker_id")
            }
            self.jobs = {
                item["job_id"]: JobInfo.model_validate(item)
                for item in raw.get("jobs", [])
                if item.get("job_id")
            }
            self.used_ports = {int(port) for port in raw.get("used_ports", [])}
            for job in self.jobs.values():
                if job.status in {JobStatus.starting, JobStatus.running}:
                    self.used_ports.add(job.gateway_port)
            logger.info(
                "loaded gateway registry state path=%s workers=%s jobs=%s used_ports=%s",
                self.state_path,
                len(self.workers),
                len(self.jobs),
                len(self.used_ports),
            )
        except Exception as exc:
            logger.exception("failed to load registry state path=%s err=%s", self.state_path, exc)
            self.workers = {}
            self.jobs = {}
            self.used_ports = set()

    def _active_job_ports_unlocked(self) -> set[int]:
        return {
            job.gateway_port
            for job in self.jobs.values()
            if job.status in {JobStatus.starting, JobStatus.running}
        }

    def _active_job_count_for_worker_unlocked(self, worker_id: str) -> int:
        return sum(
            1
            for job in self.jobs.values()
            if job.worker_id == worker_id and job.status in {JobStatus.starting, JobStatus.running}
        )

    def _effective_worker_active_jobs_unlocked(self, worker: WorkerInfo) -> int:
        return max(worker.active_jobs, self._active_job_count_for_worker_unlocked(worker.worker_id))

    def _sync_worker_active_jobs_unlocked(self, worker_id: str) -> None:
        worker = self.workers.get(worker_id)
        if not worker:
            return
        worker.active_jobs = max(worker.active_jobs, self._active_job_count_for_worker_unlocked(worker_id))

    def _rebuild_used_ports_unlocked(self) -> None:
        """Keep the reserved port set consistent with active jobs.

        Older failed/stopped job records may still contain a gateway_port that is
        currently used by another running job. A plain discard(port) can therefore
        accidentally free somebody else's active port. Rebuilding from active jobs
        prevents duplicate port assignment after deletes/reconciles/restarts.
        """
        self.used_ports = self._active_job_ports_unlocked() | {
            port
            for port in self.used_ports
            if self.port_start <= port <= self.port_end
        }

    def _save_unlocked(self) -> None:
        if not self.state_path:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema_version": 1,
                "saved_at": utcnow().isoformat(),
                "port_start": self.port_start,
                "port_end": self.port_end,
                "workers": [worker.model_dump(mode="json") for worker in self.workers.values()],
                "jobs": [job.model_dump(mode="json") for job in self.jobs.values()],
                "used_ports": sorted(self.used_ports),
            }
            tmp_path = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp_path, self.state_path)
        except Exception as exc:
            logger.exception("failed to save registry state path=%s err=%s", self.state_path, exc)

    async def persist(self) -> None:
        async with self._lock:
            self._save_unlocked()

    async def upsert_worker(self, worker: WorkerInfo) -> WorkerInfo:
        async with self._lock:
            current = self.workers.get(worker.worker_id)
            if current:
                current.address = worker.address
                current.port = worker.port
                current.capacity = worker.capacity
                current.status = WorkerStatus.online
                current.last_heartbeat = utcnow()
                self._save_unlocked()
                return current
            worker.last_heartbeat = utcnow()
            worker.status = WorkerStatus.online
            self.workers[worker.worker_id] = worker
            self._save_unlocked()
            return worker

    async def heartbeat(self, worker_id: str, active_jobs: int, capacity: int, status: WorkerStatus) -> WorkerInfo:
        async with self._lock:
            if worker_id not in self.workers:
                raise KeyError(worker_id)
            worker = self.workers[worker_id]
            worker.capacity = capacity
            worker.status = status
            worker.last_heartbeat = utcnow()
            worker.active_jobs = max(active_jobs, self._active_job_count_for_worker_unlocked(worker_id))
            self._save_unlocked()
            return worker

    async def mark_stale_workers(self) -> None:
        async with self._lock:
            changed = False
            now = utcnow()
            for worker in self.workers.values():
                age = (now - worker.last_heartbeat).total_seconds()
                if age > self.worker_stale_seconds and worker.status != WorkerStatus.offline:
                    worker.status = WorkerStatus.offline
                    changed = True
            if changed:
                self._save_unlocked()

    async def reserve_worker(self) -> WorkerInfo:
        """Atomically choose a worker and reserve one capacity slot in gateway memory.

        Heartbeats remain the source of truth over time, but the reservation prevents
        a burst of POST /api/jobs requests from all choosing the same worker before
        the next heartbeat arrives.
        """
        await self.mark_stale_workers()
        async with self._lock:
            candidates = [
                w for w in self.workers.values()
                if w.status == WorkerStatus.online
                and self._effective_worker_active_jobs_unlocked(w) < w.capacity
            ]
            if not candidates:
                raise RuntimeError("no available worker")

            def score(worker: WorkerInfo) -> tuple[float, int, str]:
                active = self._effective_worker_active_jobs_unlocked(worker)
                load = active / max(worker.capacity, 1)
                return (load, active, worker.worker_id)

            selected = sorted(candidates, key=score)[0]
            selected.active_jobs = self._effective_worker_active_jobs_unlocked(selected) + 1
            self._save_unlocked()
            return selected

    async def adjust_worker_active_jobs(self, worker_id: str, delta: int) -> None:
        async with self._lock:
            worker = self.workers.get(worker_id)
            if worker:
                gateway_known = self._active_job_count_for_worker_unlocked(worker_id)
                worker.active_jobs = max(worker.active_jobs + delta, gateway_known, 0)
                self._save_unlocked()

    async def choose_worker(self) -> WorkerInfo:
        await self.mark_stale_workers()
        async with self._lock:
            candidates = [
                w for w in self.workers.values()
                if w.status == WorkerStatus.online
                and self._effective_worker_active_jobs_unlocked(w) < w.capacity
            ]
            if not candidates:
                raise RuntimeError("no available worker")

            def score(worker: WorkerInfo) -> tuple[float, int, str]:
                active = self._effective_worker_active_jobs_unlocked(worker)
                load = active / max(worker.capacity, 1)
                return (load, active, worker.worker_id)

            return sorted(candidates, key=score)[0]

    async def allocate_port(self) -> int:
        async with self._lock:
            for port in range(self.port_start, self.port_end + 1):
                if port not in self.used_ports:
                    self.used_ports.add(port)
                    self._save_unlocked()
                    return port
            raise RuntimeError("no free gateway ports")

    async def release_port(self, port: int) -> None:
        async with self._lock:
            self.used_ports.discard(port)
            self.used_ports |= self._active_job_ports_unlocked()
            self._save_unlocked()

    async def sync_ports_and_worker_counts(self) -> None:
        """Rebuild volatile registry counters from current active jobs only."""
        async with self._lock:
            self.used_ports = self._active_job_ports_unlocked()
            for worker in self.workers.values():
                worker.active_jobs = self._active_job_count_for_worker_unlocked(worker.worker_id)
            self._save_unlocked()

    async def clear_jobs_and_ports(self) -> None:
        """Clear all job records and reserved ports after a full cleanup."""
        async with self._lock:
            self.jobs.clear()
            self.used_ports.clear()
            for worker in self.workers.values():
                worker.active_jobs = 0
            self._save_unlocked()

    async def add_job(self, job: JobInfo) -> JobInfo:
        async with self._lock:
            self.jobs[job.job_id] = job
            if job.status in {JobStatus.starting, JobStatus.running}:
                self.used_ports.add(job.gateway_port)
            self._sync_worker_active_jobs_unlocked(job.worker_id)
            self._save_unlocked()
            return job

    async def save_job(self, job: JobInfo) -> JobInfo:
        async with self._lock:
            if job.job_id not in self.jobs:
                raise KeyError(job.job_id)
            job.updated_at = utcnow()
            self.jobs[job.job_id] = job
            if job.status in {JobStatus.starting, JobStatus.running}:
                self.used_ports.add(job.gateway_port)
            else:
                self.used_ports.discard(job.gateway_port)
                self.used_ports |= self._active_job_ports_unlocked()
            self._sync_worker_active_jobs_unlocked(job.worker_id)
            self._save_unlocked()
            return job

    async def update_job_status(self, job_id: str, status: JobStatus, error: str | None = None) -> JobInfo:
        async with self._lock:
            if job_id not in self.jobs:
                raise KeyError(job_id)
            job = self.jobs[job_id]
            old_status = job.status
            job.status = status
            job.updated_at = utcnow()
            job.error = error
            if status in {JobStatus.starting, JobStatus.running}:
                self.used_ports.add(job.gateway_port)
            else:
                self.used_ports.discard(job.gateway_port)
                self.used_ports |= self._active_job_ports_unlocked()
            self._sync_worker_active_jobs_unlocked(job.worker_id)
            self._save_unlocked()
            return job

    async def remove_job(self, job_id: str) -> JobInfo:
        async with self._lock:
            if job_id not in self.jobs:
                raise KeyError(job_id)
            job = self.jobs.pop(job_id)
            self.used_ports.discard(job.gateway_port)
            self.used_ports |= self._active_job_ports_unlocked()
            worker = self.workers.get(job.worker_id)
            if worker:
                worker.active_jobs = self._active_job_count_for_worker_unlocked(job.worker_id)
            self._save_unlocked()
            return job

    async def get_job(self, job_id: str) -> JobInfo:
        async with self._lock:
            if job_id not in self.jobs:
                raise KeyError(job_id)
            return self.jobs[job_id]

    async def get_worker(self, worker_id: str) -> WorkerInfo | None:
        async with self._lock:
            return self.workers.get(worker_id)

    async def list_jobs(self) -> list[JobInfo]:
        async with self._lock:
            return sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True)

    async def list_workers(self) -> list[WorkerInfo]:
        await self.mark_stale_workers()
        async with self._lock:
            for worker in self.workers.values():
                worker.active_jobs = self._effective_worker_active_jobs_unlocked(worker)
            self._save_unlocked()
            return sorted(self.workers.values(), key=lambda w: w.worker_id)

    async def running_jobs(self) -> Iterable[JobInfo]:
        async with self._lock:
            return [j for j in self.jobs.values() if j.status == JobStatus.running]
