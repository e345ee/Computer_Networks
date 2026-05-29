from prometheus_client import Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST
from starlette.responses import Response

jobs_started_total = Counter("worker_jobs_started_total", "Total jobs started by this worker", ["worker_id"])
jobs_deleted_total = Counter("worker_jobs_deleted_total", "Total jobs deleted by this worker", ["worker_id"])
jobs_failed_total = Counter("worker_jobs_failed_total", "Total jobs failed on this worker", ["worker_id"])
active_jobs_gauge = Gauge("worker_active_jobs", "Currently running jobs", ["worker_id"])
capacity_gauge = Gauge("worker_capacity", "Worker capacity", ["worker_id"])
cpu_usage_gauge = Gauge("worker_cpu_usage_percent", "Worker CPU usage percent", ["worker_id"])
memory_usage_gauge = Gauge("worker_memory_usage_percent", "Worker memory usage percent", ["worker_id"])
containers_running_gauge = Gauge("worker_containers_running", "Known running job containers", ["worker_id"])


def metrics_response() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
