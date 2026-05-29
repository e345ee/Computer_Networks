from prometheus_client import Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST
from starlette.responses import Response

jobs_created_total = Counter("gateway_jobs_created_total", "Total jobs created through gateway")
jobs_failed_total = Counter("gateway_jobs_failed_total", "Total jobs that failed during creation or runtime")
jobs_deleted_total = Counter("gateway_jobs_deleted_total", "Total jobs deleted through gateway")
active_jobs_gauge = Gauge("gateway_active_jobs", "Currently running jobs")
workers_online_gauge = Gauge("gateway_workers_online", "Workers with online status")
workers_total_gauge = Gauge("gateway_workers_total", "Known workers")
open_ports_gauge = Gauge("gateway_open_ports", "Currently opened TCP proxy ports")
worker_active_jobs_gauge = Gauge("gateway_worker_active_jobs", "Active jobs by worker", ["worker_id"])
worker_capacity_gauge = Gauge("gateway_worker_capacity", "Configured worker capacity", ["worker_id"])


def metrics_response() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
