import { useEffect, useMemo, useState } from 'react';
import { api, HealthStatus, Job, ReadyStatus, SystemStatus, UiSnapshot, Worker } from './api';

type Tab = 'dashboard' | 'workers' | 'jobs' | 'rest';
type WsState = 'connecting' | 'connected' | 'disconnected';

function StatusBadge({ value }: { value: string }) {
  const good = value === 'online' || value === 'running' || value === 'connected' || value === 'ok' || value === 'ready';
  const bad = value === 'offline' || value === 'failed' || value === 'stopped' || value === 'disconnected' || value === 'not_ready';

  return (
    <span
      className={[
        'inline-block border px-2 py-1 text-xs font-bold uppercase',
        good ? 'border-green-700 bg-green-50 text-green-700' : '',
        bad ? 'border-red-700 bg-red-50 text-red-700' : '',
        !good && !bad ? 'border-black bg-white text-black' : ''
      ].join(' ')}
    >
      {value}
    </span>
  );
}

function StatBlock({ title, value }: { title: string; value: string | number }) {
  return (
    <div className="border border-black bg-white p-4">
      <div className="text-xs font-bold uppercase">{title}</div>
      <div className="mt-2 text-2xl font-bold">{value}</div>
    </div>
  );
}

function formatDate(value?: string) {
  if (!value) return '-';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '-' : date.toLocaleString();
}

function WorkerBlock({ worker }: { worker: Worker }) {
  const load = worker.capacity ? Math.round((worker.active_jobs / worker.capacity) * 100) : 0;
  const online = worker.status === 'online';

  return (
    <article className="border border-black bg-white p-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h3 className="font-bold">{worker.worker_id}</h3>
          <p className="mt-1 font-mono text-sm">{worker.address}:{worker.port}</p>
        </div>
        <StatusBadge value={worker.status} />
      </div>

      <div className="mt-4">
        <div className="flex justify-between text-sm">
          <span>Jobs</span>
          <span className="font-mono">{worker.active_jobs}/{worker.capacity}</span>
        </div>
        <div className="mt-2 h-3 border border-black bg-white">
          <div
            className={online ? 'h-full bg-green-700' : 'h-full bg-red-700'}
            style={{ width: `${Math.min(load, 100)}%` }}
          />
        </div>
      </div>

      <p className="mt-4 text-xs">Last heartbeat: {formatDate(worker.last_heartbeat)}</p>
    </article>
  );
}

function connectionLabel(port: number) {
  const host = window.location.hostname || 'localhost';
  return `${host}:${port}`;
}

function JobsTable({ jobs, onDelete }: { jobs: Job[]; onDelete: (jobId: string) => void }) {
  if (!jobs.length) {
    return <div className="border border-black bg-white p-6 text-center">No jobs</div>;
  }

  return (
    <div className="overflow-x-auto border border-black bg-white">
      <table className="min-w-full border-collapse text-left text-sm">
        <thead>
          <tr className="border-b border-black bg-white">
            <th className="p-3 font-bold uppercase">Job</th>
            <th className="p-3 font-bold uppercase">Status</th>
            <th className="p-3 font-bold uppercase">Worker</th>
            <th className="p-3 font-bold uppercase">Address</th>
            <th className="p-3 font-bold uppercase">Container</th>
            <th className="p-3 font-bold uppercase">Updated</th>
            <th className="p-3 font-bold uppercase">Error</th>
            <th className="p-3 text-right font-bold uppercase">Action</th>
          </tr>
        </thead>
        <tbody>
          {jobs.map((job) => (
            <tr key={job.job_id} className="border-b border-black last:border-b-0">
              <td className="max-w-xs break-all p-3 font-mono text-xs">{job.job_id}</td>
              <td className="p-3"><StatusBadge value={job.status} /></td>
              <td className="p-3">{job.worker_id}</td>
              <td className="p-3 font-mono text-xs">tcp://{connectionLabel(job.gateway_port)}</td>
              <td className="max-w-xs break-all p-3 font-mono text-xs">{job.container_name || '-'}</td>
              <td className="p-3 text-xs">{formatDate(job.updated_at)}</td>
              <td className="max-w-xs break-all p-3 text-xs text-red-700">{job.error || '-'}</td>
              <td className="p-3 text-right">
                <button
                  onClick={() => onDelete(job.job_id)}
                  className="border border-black bg-white px-3 py-2 font-bold text-black"
                >
                  Delete
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function EndpointRow({ method, path, onClick, danger }: {
  method: string;
  path: string;
  onClick: () => void;
  danger?: boolean;
}) {
  return (
    <tr className="border-b border-black last:border-b-0">
      <td className="p-3 font-mono text-xs font-bold">{method}</td>
      <td className="p-3 font-mono text-xs">{path}</td>
      <td className="p-3 text-right">
        <button
          onClick={onClick}
          className={[
            'border border-black px-3 py-2 font-bold',
            danger ? 'bg-red-700 text-white' : 'bg-white text-black'
          ].join(' ')}
        >
          Run
        </button>
      </td>
    </tr>
  );
}

export default function App() {
  const [tab, setTab] = useState<Tab>('dashboard');
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [workers, setWorkers] = useState<Worker[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [ready, setReady] = useState<ReadyStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState('');
  const [adminResult, setAdminResult] = useState('');
  const [wsState, setWsState] = useState<WsState>('connecting');
  const [lastUpdate, setLastUpdate] = useState<string>('');

  const runningJobs = useMemo(() => jobs.filter((job) => job.status === 'running'), [jobs]);

  function applySnapshot(snapshot: UiSnapshot) {
    setStatus(snapshot.system);
    setWorkers(snapshot.workers);
    setJobs(snapshot.jobs);
    setLastUpdate(snapshot.server_time);
  }

  function showAdminResult(value: unknown) {
    setAdminResult(JSON.stringify(value, null, 2));
  }

  async function refresh() {
    setLoading(true);
    try {
      const [systemData, workersData, jobsData] = await Promise.all([api.system(), api.workers(), api.jobs()]);
      setStatus(systemData);
      setWorkers(workersData);
      setJobs(jobsData);
      setLastUpdate(new Date().toISOString());
      setMessage('Refreshed');
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Unknown error');
    } finally {
      setLoading(false);
    }
  }

  async function createJob() {
    setLoading(true);
    try {
      const result = await api.createJob();
      setMessage(`Created ${result.job_id} on TCP port ${result.port}`);
      setTab('jobs');
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Unknown error');
    } finally {
      setLoading(false);
    }
  }

  async function deleteJob(jobId: string) {
    setLoading(true);
    try {
      await api.deleteJob(jobId);
      setMessage(`Deleted ${jobId}`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Unknown error');
    } finally {
      setLoading(false);
    }
  }

  async function runHealth() {
    const result = await api.health();
    setHealth(result);
    showAdminResult(result);
  }

  async function runReady() {
    const result = await api.ready();
    setReady(result);
    showAdminResult(result);
  }

  async function runReconcile() {
    const result = await api.reconcile();
    showAdminResult(result);
    setMessage(`Reconciled ${result.reconciled_jobs} jobs, updated ${result.updated_jobs}`);
  }

  async function runWorkers() {
    const result = await api.workers();
    setWorkers(result);
    showAdminResult(result);
  }

  async function runJobs() {
    const result = await api.jobs();
    setJobs(result);
    showAdminResult(result);
  }

  async function runDeleteAllJobs() {
    if (!window.confirm('Delete all jobs?')) {
      return;
    }
    const result = await api.deleteAllJobs();
    showAdminResult(result);
    setMessage(`Deleted ${result.deleted} jobs`);
  }

  async function runAdminAction(action: () => Promise<void>) {
    setLoading(true);
    try {
      await action();
    } catch (error) {
      const text = error instanceof Error ? error.message : 'Unknown error';
      setMessage(text);
      setAdminResult(text);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    api.health().then(setHealth).catch(() => undefined);
    api.ready().then(setReady).catch(() => undefined);
  }, []);

  useEffect(() => {
    let stopped = false;
    let socket: WebSocket | null = null;
    let reconnectTimer: number | undefined;
    let reconnectDelayMs = 1000;

    function connect() {
      if (stopped) return;
      setWsState('connecting');
      socket = new WebSocket(api.updatesWebSocketUrl());

      socket.onopen = () => {
        reconnectDelayMs = 1000;
        setWsState('connected');
      };

      socket.onmessage = (event) => {
        try {
          const payload = JSON.parse(event.data) as UiSnapshot;
          if (payload.type === 'snapshot') {
            applySnapshot(payload);
          }
        } catch (error) {
          setMessage(error instanceof Error ? error.message : 'Failed to parse WebSocket update');
        }
      };

      socket.onerror = () => {
        setWsState('disconnected');
      };

      socket.onclose = () => {
        if (stopped) return;
        setWsState('disconnected');
        reconnectTimer = window.setTimeout(connect, reconnectDelayMs);
        reconnectDelayMs = Math.min(reconnectDelayMs * 2, 5000);
      };
    }

    connect();

    return () => {
      stopped = true;
      if (reconnectTimer) {
        window.clearTimeout(reconnectTimer);
      }
      if (socket) {
        socket.close();
      }
    };
  }, []);

  return (
    <main className="min-h-screen bg-white text-black">
      <div className="mx-auto max-w-7xl p-6">
        <header className="border border-black bg-white p-5">
          <div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
            <div>
              <div className="text-xs font-bold uppercase">Distributed Computing</div>
              <h1 className="mt-2 text-3xl font-bold">Gateway Control Panel</h1>
              <div className="mt-3 flex flex-wrap items-center gap-3 text-sm">
                <span>WebSocket:</span>
                <StatusBadge value={wsState} />
                <span>Last update: {lastUpdate ? new Date(lastUpdate).toLocaleString() : '-'}</span>
              </div>
            </div>
            <div className="flex flex-wrap gap-2">
              <button onClick={refresh} className="border border-black bg-white px-4 py-2 font-bold">
                {loading ? 'Refreshing...' : 'Manual refresh'}
              </button>
              <button onClick={createJob} className="border border-black bg-black px-4 py-2 font-bold text-white">
                Create job
              </button>
            </div>
          </div>
        </header>

        {message && <div className="mt-4 border border-black bg-white p-3 font-mono text-sm">{message}</div>}

        <nav className="mt-4 flex flex-wrap gap-2">
          {(['dashboard', 'workers', 'jobs', 'rest'] as Tab[]).map((item) => (
            <button
              key={item}
              onClick={() => setTab(item)}
              className={[
                'border border-black px-4 py-2 font-bold capitalize',
                tab === item ? 'bg-black text-white' : 'bg-white text-black'
              ].join(' ')}
            >
              {item}
            </button>
          ))}
        </nav>

        {tab === 'dashboard' && (
          <section className="mt-4 space-y-4">
            <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
              <StatBlock title="Gateway" value={status?.gateway || 'online'} />
              <StatBlock title="Workers online" value={`${status?.workers_online ?? 0}/${status?.workers_total ?? 0}`} />
              <StatBlock title="Active jobs" value={status?.active_jobs ?? runningJobs.length} />
              <StatBlock title="Free capacity" value={`${status?.free_capacity ?? 0}/${status?.total_capacity ?? 0}`} />
            </div>

            <div className="border border-black bg-white p-4">
              <h2 className="mb-4 text-xl font-bold">REST health</h2>
              <div className="flex flex-wrap gap-3 text-sm">
                <span>GET /health:</span>
                <StatusBadge value={health?.status || 'unknown'} />
                <span>GET /ready:</span>
                <StatusBadge value={ready?.status || 'unknown'} />
              </div>
            </div>

            <div className="border border-black bg-white p-4">
              <h2 className="mb-4 text-xl font-bold">Workers</h2>
              <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
                {workers.map((worker) => <WorkerBlock key={worker.worker_id} worker={worker} />)}
              </div>
            </div>
          </section>
        )}

        {tab === 'workers' && (
          <section className="mt-4 grid gap-4 md:grid-cols-2 lg:grid-cols-3">
            {workers.map((worker) => <WorkerBlock key={worker.worker_id} worker={worker} />)}
          </section>
        )}

        {tab === 'jobs' && (
          <section className="mt-4 space-y-4">
            <JobsTable jobs={jobs} onDelete={deleteJob} />
          </section>
        )}

        {tab === 'rest' && (
          <section className="mt-4 space-y-4">
            <div className="border border-black bg-white p-4">
              <h2 className="text-xl font-bold">REST endpoints</h2>
            </div>

            <div className="overflow-x-auto border border-black bg-white">
              <table className="min-w-full border-collapse text-left">
                <thead>
                  <tr className="border-b border-black">
                    <th className="p-3 text-xs font-bold uppercase">Method</th>
                    <th className="p-3 text-xs font-bold uppercase">Endpoint</th>
                    <th className="p-3 text-right text-xs font-bold uppercase">Action</th>
                  </tr>
                </thead>
                <tbody>
                  <EndpointRow method="GET" path="/health" onClick={() => runAdminAction(runHealth)} />
                  <EndpointRow method="GET" path="/ready" onClick={() => runAdminAction(runReady)} />
                  <EndpointRow method="POST" path="/api/reconcile" onClick={() => runAdminAction(runReconcile)} />
                  <EndpointRow method="GET" path="/api/workers" onClick={() => runAdminAction(runWorkers)} />
                  <EndpointRow method="GET" path="/api/jobs" onClick={() => runAdminAction(runJobs)} />
                  <EndpointRow method="DELETE" path="/api/jobs" onClick={() => runAdminAction(runDeleteAllJobs)} danger />
                </tbody>
              </table>
            </div>

            <div className="border border-black bg-white p-4">
              <h3 className="font-bold">Last REST result</h3>
              <pre className="mt-3 max-h-96 overflow-auto border border-black bg-white p-3 text-xs">
                {adminResult || '-'}
              </pre>
            </div>
          </section>
        )}
      </div>
    </main>
  );
}
