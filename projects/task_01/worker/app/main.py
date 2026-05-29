import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
import psutil
from fastapi import FastAPI, HTTPException
from pythonjsonlogger import jsonlogger

from . import metrics
from .docker_runtime import DockerRuntime
from .models import CreateInternalJobRequest, JobStatus, WorkerJob


def setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler()
    handler.setFormatter(jsonlogger.JsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=level, handlers=[handler], force=True)


setup_logging()
logger = logging.getLogger("worker")

WORKER_ID = os.getenv("WORKER_ID", "worker-1")
WORKER_ADDRESS = os.getenv("WORKER_ADDRESS", WORKER_ID)
WORKER_API_PORT = int(os.getenv("WORKER_API_PORT", "9000"))
WORKER_CAPACITY = int(os.getenv("WORKER_CAPACITY", "5"))
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://gateway:8080").rstrip("/")
HEARTBEAT_INTERVAL_SECONDS = int(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "5"))

jobs: dict[str, WorkerJob] = {}
runtime: DockerRuntime | None = None
heartbeat_task: asyncio.Task | None = None
jobs_lock = asyncio.Lock()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def active_jobs_count() -> int:
    return sum(1 for job in jobs.values() if job.status == JobStatus.running)


async def refresh_known_jobs() -> None:
    if runtime is None:
        return
    for job_id, job in list(jobs.items()):
        jobs[job_id] = await asyncio.to_thread(runtime.refresh_status, job)


async def update_metrics() -> None:
    metrics.active_jobs_gauge.labels(WORKER_ID).set(active_jobs_count())
    metrics.capacity_gauge.labels(WORKER_ID).set(WORKER_CAPACITY)
    metrics.cpu_usage_gauge.labels(WORKER_ID).set(psutil.cpu_percent(interval=None))
    metrics.memory_usage_gauge.labels(WORKER_ID).set(psutil.virtual_memory().percent)
    metrics.containers_running_gauge.labels(WORKER_ID).set(active_jobs_count())


async def register_with_gateway() -> None:
    payload = {
        "worker_id": WORKER_ID,
        "address": WORKER_ADDRESS,
        "port": WORKER_API_PORT,
        "capacity": WORKER_CAPACITY,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(f"{GATEWAY_URL}/internal/workers/register", json=payload)
        response.raise_for_status()
    logger.info("registered on gateway worker_id=%s gateway=%s", WORKER_ID, GATEWAY_URL)


async def heartbeat_loop() -> None:
    while True:
        try:
            await refresh_known_jobs()
            try:
                await register_with_gateway()
            except Exception:
                pass
            payload = {
                "worker_id": WORKER_ID,
                "active_jobs": active_jobs_count(),
                "capacity": WORKER_CAPACITY,
                "status": "online",
            }
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(f"{GATEWAY_URL}/internal/workers/heartbeat", json=payload)
                if response.status_code == 404:
                    await register_with_gateway()
                else:
                    response.raise_for_status()
            await update_metrics()
        except Exception as exc:
            logger.warning("heartbeat failed: %s", exc)
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global runtime, heartbeat_task
    runtime = DockerRuntime()
    recovered_jobs = await asyncio.to_thread(runtime.recover_jobs)
    jobs.update(recovered_jobs)
    heartbeat_task = asyncio.create_task(heartbeat_loop())
    logger.info(
        "worker started worker_id=%s capacity=%s recovered_jobs=%s",
        WORKER_ID,
        WORKER_CAPACITY,
        len(recovered_jobs),
    )
    yield
    if heartbeat_task:
        heartbeat_task.cancel()
    logger.info("worker stopped")


app = FastAPI(title="Distributed Computing Worker", version="1.0.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "worker", "worker_id": WORKER_ID}


@app.get("/metrics")
async def prometheus_metrics():
    await update_metrics()
    return metrics.metrics_response()


@app.get("/internal/status")
async def worker_status() -> dict:
    await refresh_known_jobs()
    await update_metrics()
    return {
        "worker_id": WORKER_ID,
        "address": WORKER_ADDRESS,
        "port": WORKER_API_PORT,
        "capacity": WORKER_CAPACITY,
        "active_jobs": active_jobs_count(),
        "jobs": [job.public_dict() for job in jobs.values()],
    }


@app.post("/internal/jobs", status_code=201)
async def create_job(payload: CreateInternalJobRequest) -> dict:
    async with jobs_lock:
        await refresh_known_jobs()
        if payload.job_id in jobs and jobs[payload.job_id].status == JobStatus.running:
            raise HTTPException(status_code=409, detail="job already exists")
        if active_jobs_count() >= WORKER_CAPACITY:
            raise HTTPException(status_code=409, detail="worker capacity exceeded")
        assert runtime is not None
        try:
            job = await asyncio.to_thread(runtime.create_job, payload.job_id)
            jobs[payload.job_id] = job
            metrics.jobs_started_total.labels(WORKER_ID).inc()
            await update_metrics()
            return job.public_dict()
        except Exception as exc:
            metrics.jobs_failed_total.labels(WORKER_ID).inc()
            raise HTTPException(status_code=500, detail=str(exc)) from exc

@app.get("/internal/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="job not found")
    assert runtime is not None
    job = await asyncio.to_thread(runtime.refresh_status, jobs[job_id])
    jobs[job_id] = job
    await update_metrics()
    return job.public_dict()


@app.delete("/internal/jobs/{job_id}")
async def delete_job(job_id: str) -> dict:
    async with jobs_lock:
        if job_id not in jobs:
            return {"job_id": job_id, "status": "stopped", "message": "job was already absent"}
        assert runtime is not None
        try:
            job = await asyncio.to_thread(runtime.delete_job, jobs[job_id])
            jobs.pop(job_id, None)
            metrics.jobs_deleted_total.labels(WORKER_ID).inc()
            await update_metrics()
            return job.public_dict()
        except Exception as exc:
            metrics.jobs_failed_total.labels(WORKER_ID).inc()
            raise HTTPException(status_code=500, detail=str(exc)) from exc
