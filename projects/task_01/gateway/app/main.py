import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pythonjsonlogger import jsonlogger

from . import metrics
from .models import (
    CreateJobResponse,
    JobInfo,
    JobStatus,
    SystemStatus,
    WorkerHeartbeatRequest,
    WorkerInfo,
    WorkerRegisterRequest,
    WorkerStatus,
)
from .proxy import TcpProxyManager
from .registry import Registry


def setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler()
    handler.setFormatter(jsonlogger.JsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=level, handlers=[handler], force=True)


setup_logging()
logger = logging.getLogger("gateway")

PORT_START = int(os.getenv("JOB_PORT_START", "5000"))
PORT_END = int(os.getenv("JOB_PORT_END", "5099"))
WORKER_STALE_SECONDS = int(os.getenv("WORKER_STALE_SECONDS", "15"))
HTTP_TIMEOUT_SECONDS = float(os.getenv("HTTP_TIMEOUT_SECONDS", "15"))
STATE_PATH = os.getenv("REGISTRY_STATE_PATH", "/data/registry.json")
RECONCILE_INTERVAL_SECONDS = int(os.getenv("RECONCILE_INTERVAL_SECONDS", "5"))

registry = Registry(PORT_START, PORT_END, WORKER_STALE_SECONDS, STATE_PATH)
proxy_manager = TcpProxyManager()
create_job_lock = asyncio.Lock()
reconcile_task: asyncio.Task | None = None


class WebSocketHub:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._last_snapshot_json = ""

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)

    async def broadcast(self, payload: dict) -> None:
        async with self._send_lock:
            async with self._lock:
                connections = list(self._connections)
            if not connections:
                return

            stale: list[WebSocket] = []
            for websocket in connections:
                try:
                    await websocket.send_json(payload)
                except Exception:
                    stale.append(websocket)

            if stale:
                async with self._lock:
                    for websocket in stale:
                        self._connections.discard(websocket)

    def snapshot_changed(self, payload: dict) -> bool:
        comparable = dict(payload)
        comparable.pop("server_time", None)
        for worker in comparable.get("workers", []):
            worker.pop("last_heartbeat", None)
        for job in comparable.get("jobs", []):
            job.pop("updated_at", None)
        current = json.dumps(comparable, sort_keys=True, ensure_ascii=False)
        if current == self._last_snapshot_json:
            return False
        self._last_snapshot_json = current
        return True


ws_hub = WebSocketHub()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def worker_status_to_gateway_status(value: str) -> JobStatus:
    try:
        return JobStatus(value)
    except ValueError:
        if value == "stopped":
            return JobStatus.stopped
        if value == "failed":
            return JobStatus.failed
        return JobStatus.running


async def ensure_proxy_for_job(job: JobInfo) -> None:
    if job.status != JobStatus.running or proxy_manager.has_proxy(job.job_id):
        return
    await proxy_manager.start_proxy(
        job_id=job.job_id,
        listen_host="0.0.0.0",
        listen_port=job.gateway_port,
        target_host=job.internal_host,
        target_port=job.internal_port,
    )


async def close_proxy_if_finished(job: JobInfo) -> None:
    if job.status in {JobStatus.stopped, JobStatus.failed}:
        await proxy_manager.stop_proxy(job.job_id)
        await registry.release_port(job.gateway_port)


async def cleanup_orphan_proxies() -> int:
    """Stop TCP proxies that no longer have a running job in the registry.

    Docker Compose publishes the whole host range 5000-5099, so `ss` on the host
    will always show docker-proxy for those ports. This function cleans only the
    real gateway asyncio proxies inside the gateway process.
    """
    jobs = await registry.list_jobs()
    keep_job_ids = {job.job_id for job in jobs if job.status == JobStatus.running}
    stopped = await proxy_manager.stop_except(keep_job_ids)
    if stopped:
        logger.warning("cleaned orphan gateway proxies count=%s", stopped)
        await registry.sync_ports_and_worker_counts()
    return stopped


async def refresh_job_from_worker(job: JobInfo, *, strict: bool = False) -> JobInfo:
    """Synchronize gateway job state with the worker/container state.

    This makes GET /api/jobs/{id}, GET /api/jobs and the frontend reflect the real
    container lifecycle, including a job-runner stopped from the TCP command line.
    """
    if job.status in {JobStatus.stopped, JobStatus.failed}:
        await close_proxy_if_finished(job)
        return job

    worker = await registry.get_worker(job.worker_id)
    if worker is None:
        if strict:
            job.error = f"worker {job.worker_id} is unknown in registry"
            await registry.save_job(job)
        return job

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(f"{worker.api_base_url}/internal/jobs/{job.job_id}")
            if response.status_code == 404:
                job.status = JobStatus.stopped
                job.error = "job is absent on worker"
            else:
                response.raise_for_status()
                worker_job = response.json()
                job.status = worker_status_to_gateway_status(str(worker_job.get("status", "running")))
                job.internal_host = worker_job.get("internal_host", job.internal_host)
                job.internal_port = int(worker_job.get("internal_port", job.internal_port))
                job.container_id = worker_job.get("container_id", job.container_id)
                job.container_name = worker_job.get("container_name", job.container_name)
                job.error = worker_job.get("error")
    except Exception as exc:
        logger.warning("failed to refresh job from worker job_id=%s worker=%s err=%s", job.job_id, job.worker_id, exc)
        if strict:
            job.error = f"failed to refresh status from worker: {exc}"
        await registry.save_job(job)
        return job

    if job.status == JobStatus.running:
        await ensure_proxy_for_job(job)
    else:
        await close_proxy_if_finished(job)
    return await registry.save_job(job)


async def refresh_all_jobs_from_workers() -> list[JobInfo]:
    jobs = await registry.list_jobs()
    refreshed: list[JobInfo] = []
    for job in jobs:
        refreshed.append(await refresh_job_from_worker(job))
    return refreshed


async def reconcile_jobs(*, strict: bool = True) -> dict:
    """Manually synchronize gateway registry with workers and containers."""
    before_jobs = {job.job_id: job.public_dict() for job in await registry.list_jobs()}
    missing_workers: set[str] = set()
    refreshed: list[JobInfo] = []

    for job in await registry.list_jobs():
        if await registry.get_worker(job.worker_id) is None:
            missing_workers.add(job.worker_id)
        refreshed.append(await refresh_job_from_worker(job, strict=strict))

    updated_jobs = 0
    for job in refreshed:
        previous = before_jobs.get(job.job_id, {})
        current = job.public_dict()
        if previous.get("status") != current.get("status") or previous.get("error") != current.get("error"):
            updated_jobs += 1

    orphan_proxies_stopped = await cleanup_orphan_proxies()
    await refresh_metrics()
    await notify_ui_clients(force=True)
    return {
        "status": "ok",
        "reconciled_jobs": len(refreshed),
        "updated_jobs": updated_jobs,
        "orphan_proxies_stopped": orphan_proxies_stopped,
        "missing_workers": len(missing_workers),
        "missing_worker_ids": sorted(missing_workers),
    }


async def restore_persisted_proxies() -> None:
    jobs = await registry.list_jobs()
    for job in jobs:
        if job.status != JobStatus.running:
            continue
        try:
            await ensure_proxy_for_job(job)
            logger.info("restored proxy for persisted job job_id=%s port=%s", job.job_id, job.gateway_port)
        except Exception as exc:
            job.status = JobStatus.failed
            job.error = f"failed to restore gateway proxy after restart: {exc}"
            await registry.save_job(job)
            metrics.jobs_failed_total.inc()
            logger.exception("failed to restore proxy job_id=%s", job.job_id)


async def reconcile_loop() -> None:
    while True:
        try:
            await refresh_all_jobs_from_workers()
            await refresh_metrics()
            await notify_ui_clients()
        except Exception as exc:
            logger.warning("gateway reconcile failed: %s", exc)
        await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global reconcile_task
    logger.info(
        "gateway started port_range=%s-%s state_path=%s",
        PORT_START,
        PORT_END,
        STATE_PATH,
    )
    await restore_persisted_proxies()
    reconcile_task = asyncio.create_task(reconcile_loop())
    yield
    if reconcile_task:
        reconcile_task.cancel()
    await proxy_manager.stop_all()
    await registry.persist()
    logger.info("gateway stopped")


app = FastAPI(title="Distributed Computing Gateway", version="1.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def build_system_status() -> SystemStatus:
    workers = await registry.list_workers()
    jobs = await registry.list_jobs()
    online_workers = [w for w in workers if w.status == WorkerStatus.online]
    active_jobs = sum(1 for j in jobs if j.status == JobStatus.running)
    total_capacity = sum(w.capacity for w in online_workers)
    used_capacity = sum(w.active_jobs for w in online_workers)
    return SystemStatus(
        workers_online=len(online_workers),
        workers_total=len(workers),
        active_jobs=active_jobs,
        total_capacity=total_capacity,
        free_capacity=max(total_capacity - used_capacity, 0),
        open_ports=len(proxy_manager.list_ports()),
    )


async def build_ui_snapshot() -> dict:
    system = await build_system_status()
    workers = await registry.list_workers()
    jobs = await registry.list_jobs()
    return {
        "type": "snapshot",
        "server_time": utcnow().isoformat(),
        "system": system.model_dump(mode="json"),
        "workers": [worker.public_dict() for worker in workers],
        "jobs": [job.public_dict() for job in jobs],
    }


async def notify_ui_clients(*, force: bool = False) -> None:
    payload = await build_ui_snapshot()
    if force or ws_hub.snapshot_changed(payload):
        await ws_hub.broadcast(payload)


async def refresh_metrics() -> None:
    workers = await registry.list_workers()
    jobs = await registry.list_jobs()
    active_jobs = sum(1 for j in jobs if j.status == JobStatus.running)
    online_workers = sum(1 for w in workers if w.status == WorkerStatus.online)
    metrics.active_jobs_gauge.set(active_jobs)
    metrics.workers_online_gauge.set(online_workers)
    metrics.workers_total_gauge.set(len(workers))
    metrics.open_ports_gauge.set(len(proxy_manager.list_ports()))
    for worker in workers:
        metrics.worker_active_jobs_gauge.labels(worker.worker_id).set(worker.active_jobs)
        metrics.worker_capacity_gauge.labels(worker.worker_id).set(worker.capacity)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "gateway"}


@app.get("/api/health")
async def api_health() -> dict:
    return await health()


@app.get("/ready")
async def ready() -> dict:
    workers = await registry.list_workers()
    online_workers = [worker for worker in workers if worker.status == WorkerStatus.online]
    total_capacity = sum(worker.capacity for worker in online_workers)
    used_capacity = sum(worker.active_jobs for worker in online_workers)
    free_capacity = max(total_capacity - used_capacity, 0)
    ready_state = len(online_workers) > 0 and free_capacity > 0
    return {
        "ready": ready_state,
        "status": "ready" if ready_state else "not_ready",
        "service": "gateway",
        "online_workers": len(online_workers),
        "workers_total": len(workers),
        "free_capacity": free_capacity,
        "total_capacity": total_capacity,
        "reason": None if ready_state else "no online workers or no free capacity",
    }


@app.get("/api/ready")
async def api_ready() -> dict:
    return await ready()


@app.websocket("/ws/updates")
async def websocket_updates(websocket: WebSocket) -> None:
    await ws_hub.connect(websocket)
    try:
        await notify_ui_clients(force=True)
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await ws_hub.disconnect(websocket)


@app.get("/metrics")
async def prometheus_metrics():
    await refresh_metrics()
    return metrics.metrics_response()


@app.post("/internal/workers/register")
async def register_worker(payload: WorkerRegisterRequest) -> dict:
    worker = WorkerInfo(
        worker_id=payload.worker_id,
        address=payload.address,
        port=payload.port,
        capacity=payload.capacity,
        active_jobs=0,
        status=WorkerStatus.online,
        last_heartbeat=utcnow(),
    )
    saved = await registry.upsert_worker(worker)
    await refresh_metrics()
    await notify_ui_clients()
    logger.info("worker registered worker_id=%s address=%s:%s", saved.worker_id, saved.address, saved.port)
    return saved.public_dict()


@app.post("/internal/workers/heartbeat")
async def worker_heartbeat(payload: WorkerHeartbeatRequest) -> dict:
    try:
        worker = await registry.heartbeat(
            payload.worker_id,
            payload.active_jobs,
            payload.capacity,
            payload.status,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="worker is not registered") from None
    await refresh_metrics()
    await notify_ui_clients()
    return worker.public_dict()


@app.get("/api/workers")
async def list_workers() -> list[dict]:
    workers = await registry.list_workers()
    await refresh_metrics()
    return [w.public_dict() for w in workers]


@app.get("/api/jobs")
async def list_jobs() -> list[dict]:
    jobs = await refresh_all_jobs_from_workers()
    await refresh_metrics()
    return [j.public_dict() for j in jobs]


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    try:
        job = await registry.get_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found") from None
    job = await refresh_job_from_worker(job, strict=True)
    await refresh_metrics()
    return job.public_dict()


@app.get("/api/system/status", response_model=SystemStatus)
async def system_status() -> SystemStatus:
    await refresh_metrics()
    return await build_system_status()


@app.post("/api/reconcile")
async def reconcile() -> dict:
    return await reconcile_jobs(strict=True)


@app.delete("/api/jobs")
async def delete_all_jobs() -> dict:
    async with create_job_lock:
        jobs = await registry.list_jobs()
        deleted = 0
        failed: list[dict] = []

        for job in jobs:
            try:
                worker = await registry.get_worker(job.worker_id)
                if worker:
                    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                        try:
                            await client.delete(f"{worker.api_base_url}/internal/jobs/{job.job_id}")
                        except Exception as exc:
                            logger.warning(
                                "failed to delete job on worker during bulk delete job_id=%s worker=%s err=%s",
                                job.job_id,
                                worker.worker_id,
                                exc,
                            )
                metrics.jobs_deleted_total.inc()
                deleted += 1
            except Exception as exc:
                logger.exception("failed to delete job during bulk delete job_id=%s", job.job_id)
                failed.append({"job_id": job.job_id, "error": str(exc)})

        stopped_proxies = await proxy_manager.stop_all()
        await registry.clear_jobs_and_ports()

        await refresh_metrics()
        await notify_ui_clients(force=True)
        return {
            "status": "ok" if not failed else "partial",
            "deleted": deleted,
            "stopped_proxies": stopped_proxies,
            "failed": failed,
        }


@app.post("/api/jobs", response_model=CreateJobResponse, status_code=201)
async def create_job() -> CreateJobResponse:
    async with create_job_lock:
        await cleanup_orphan_proxies()
        job_id = f"job-{uuid.uuid4().hex[:10]}"
        gateway_port: int | None = None
        worker: WorkerInfo | None = None
        worker_job: dict | None = None
        job_registered = False

        try:
            worker = await registry.reserve_worker()
            gateway_port = await registry.allocate_port()
            logger.info("creating job job_id=%s worker=%s gateway_port=%s", job_id, worker.worker_id, gateway_port)

            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                response = await client.post(f"{worker.api_base_url}/internal/jobs", json={"job_id": job_id})
                response.raise_for_status()
                worker_job = response.json()

            job = JobInfo(
                job_id=job_id,
                status=JobStatus.starting,
                worker_id=worker.worker_id,
                gateway_port=gateway_port,
                internal_host=worker_job["internal_host"],
                internal_port=int(worker_job["internal_port"]),
                container_id=worker_job.get("container_id"),
                container_name=worker_job.get("container_name"),
            )
            await registry.add_job(job)
            job_registered = True

            await proxy_manager.start_proxy(
                job_id=job_id,
                listen_host="0.0.0.0",
                listen_port=gateway_port,
                target_host=job.internal_host,
                target_port=job.internal_port,
            )
            await registry.update_job_status(job_id, JobStatus.running)
            metrics.jobs_created_total.inc()
            await refresh_metrics()
            await notify_ui_clients()
            return CreateJobResponse(
                job_id=job_id,
                status=JobStatus.running,
                worker_id=worker.worker_id,
                port=gateway_port,
                protocol="tcp",
                connect=f"nc <gateway_ip> {gateway_port}",
            )
        except httpx.HTTPStatusError as exc:
            metrics.jobs_failed_total.inc()
            if worker:
                await registry.adjust_worker_active_jobs(worker.worker_id, -1)
            if gateway_port:
                await registry.release_port(gateway_port)
            if job_registered:
                try:
                    await registry.remove_job(job_id)
                except KeyError:
                    pass
            detail = f"worker returned error: {exc.response.text}"
            logger.exception("failed to create job job_id=%s detail=%s", job_id, detail)
            await refresh_metrics()
            await notify_ui_clients(force=True)
            raise HTTPException(status_code=502, detail=detail) from exc
        except Exception as exc:
            metrics.jobs_failed_total.inc()
            if worker:
                await registry.adjust_worker_active_jobs(worker.worker_id, -1)
            if gateway_port:
                await registry.release_port(gateway_port)
            if worker and worker_job:
                async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                    try:
                        await client.delete(f"{worker.api_base_url}/internal/jobs/{job_id}")
                    except Exception:
                        pass
            if job_registered:
                try:
                    await registry.remove_job(job_id)
                except KeyError:
                    pass
            logger.exception("failed to create job job_id=%s", job_id)
            await refresh_metrics()
            await notify_ui_clients(force=True)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str) -> dict:
    try:
        job = await registry.get_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found") from None

    previous_status = job.status
    await proxy_manager.stop_proxy(job_id)
    worker = await registry.get_worker(job.worker_id)
    if worker:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            try:
                await client.delete(f"{worker.api_base_url}/internal/jobs/{job_id}")
            except Exception as exc:
                logger.warning("failed to delete job on worker job_id=%s worker=%s err=%s", job_id, worker.worker_id, exc)

    await registry.release_port(job.gateway_port)
    if previous_status in {JobStatus.starting, JobStatus.running}:
        await registry.adjust_worker_active_jobs(job.worker_id, -1)
    try:
        removed = await registry.remove_job(job_id)
    except KeyError:
        removed = job
    removed.status = JobStatus.stopped
    metrics.jobs_deleted_total.inc()
    await refresh_metrics()
    await notify_ui_clients(force=True)
    return removed.public_dict()
