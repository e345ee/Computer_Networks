export type Worker = {
  worker_id: string;
  address: string;
  port: number;
  capacity: number;
  active_jobs: number;
  status: 'online' | 'offline';
  last_heartbeat: string;
  free_capacity: number;
  api_base_url: string;
};

export type Job = {
  job_id: string;
  status: 'starting' | 'running' | 'stopped' | 'failed';
  worker_id: string;
  protocol: string;
  gateway_port: number;
  internal_host: string;
  internal_port: number;
  container_id?: string;
  container_name?: string;
  created_at: string;
  updated_at: string;
  error?: string;
};

export type SystemStatus = {
  gateway: string;
  workers_online: number;
  workers_total: number;
  active_jobs: number;
  total_capacity: number;
  free_capacity: number;
  open_ports: number;
};

export type HealthStatus = {
  status: string;
  service: string;
};

export type ReadyStatus = {
  ready: boolean;
  status: 'ready' | 'not_ready';
  service: string;
  online_workers: number;
  workers_total: number;
  free_capacity: number;
  total_capacity: number;
  reason?: string | null;
};

export type ReconcileResponse = {
  status: string;
  reconciled_jobs: number;
  updated_jobs: number;
  missing_workers: number;
  missing_worker_ids: string[];
};

export type DeleteAllJobsResponse = {
  status: string;
  deleted: number;
  failed: Array<{ job_id: string; error: string }>;
};

export type CreateJobResponse = {
  job_id: string;
  status: string;
  worker_id: string;
  port: number;
  protocol: string;
  connect: string;
};

export type UiSnapshot = {
  type: 'snapshot';
  server_time: string;
  system: SystemStatus;
  workers: Worker[];
  jobs: Job[];
};

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...options
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `Request failed: ${response.status}`);
  }
  return response.json() as Promise<T>;
}

function websocketUrl(path: string): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}${path}`;
}

export const api = {
  health: () => request<HealthStatus>('/health'),
  ready: () => request<ReadyStatus>('/ready'),
  system: () => request<SystemStatus>('/api/system/status'),
  workers: () => request<Worker[]>('/api/workers'),
  jobs: () => request<Job[]>('/api/jobs'),
  reconcile: () => request<ReconcileResponse>('/api/reconcile', { method: 'POST' }),
  createJob: () => request<CreateJobResponse>('/api/jobs', { method: 'POST' }),
  deleteJob: (jobId: string) => request<Job>(`/api/jobs/${jobId}`, { method: 'DELETE' }),
  deleteAllJobs: () => request<DeleteAllJobsResponse>('/api/jobs', { method: 'DELETE' }),
  updatesWebSocketUrl: () => websocketUrl('/ws/updates')
};
