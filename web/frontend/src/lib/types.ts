/** Shared API contract types (mirror docs/api-contract.md). */

export interface EndpointOut {
  id: string;
  name: string;
  /** Routing alias: clients send this as the "model" to reach this server. */
  alias: string | null;
  model_override: string | null;
  kind: 'local' | 'remote_direct' | 'remote_tunnel';
  server_type: 'llama.cpp' | 'vLLM' | 'ollama' | 'openai' | 'opencode';
  /** Wire protocol relay speaks to this upstream. Non-'openai' endpoints are
   * alias-only: never chosen for "auto" or as a failover target. */
  protocol: 'openai' | 'opencode';
  /** Explicit model ids this endpoint serves. Each one is advertised in
   * GET /v1/models and routes here with that exact model. */
  available_models: string[];
  base_url: string;
  has_key: boolean;
  tunnel_id: string | null;
  /** Raw `ssh -N -L ...` command for a tunnel-backed endpoint (manual connect). */
  tunnel_command: string | null;
  tunnel_local_port: number | null;
  priority: number;
  weight: number;
  enabled: boolean;
  model: string | null;
  health: 'healthy' | 'degraded' | 'failed' | 'unknown';
  ewma_latency_ms: number | null;
  last_ok_ts: number | null;
  consecutive_fails: number;
  active: boolean;
  share: number;
  /** Set when another program registered this endpoint via
   * PUT /admin/registrations/{key} (e.g. "lcpp"); null for hand-added ones. */
  owner: string | null;
  external_key: string | null;
  /** The owner's own data, e.g. { manager_url } for lcpp. */
  meta: Record<string, unknown> | null;
}

export interface RegistrationOut {
  key: string;
  owner: string | null;
  adopted: boolean;
  skipped_models: string[];
  endpoint: EndpointOut;
}

export interface EndpointTestResult {
  ok: boolean;
  latency_ms: number | null;
  models: string[];
  error: string | null;
}

export interface TunnelOut {
  id: string;
  name: string;
  ssh_host: string;
  ssh_port: number;
  ssh_user: string;
  key_path: string | null;
  remote_host: string;
  remote_port: number;
  local_port: number;
  compress: boolean;
  keepalive: boolean;
  extra_opts: string | null;
  enabled: boolean;
  status: 'stopped' | 'starting' | 'up' | 'error';
  pid: number | null;
  last_error: string | null;
  started_at: number | null;
  uptime_s: number;
}

export type TunnelSessionState =
  | 'idle' | 'connecting' | 'awaiting_input' | 'up' | 'error' | 'stopped';

/** Live state of an endpoint's interactive SSH tunnel session. */
export interface TunnelSessionStatus {
  endpoint_id: string;
  status: TunnelSessionState;
  prompt: string | null;
  prompt_secret: boolean;
  output: string[];
  last_error: string | null;
  local_port: number | null;
  pid: number | null;
  uptime_s: number;
}

/** A `~/.ssh/config` host alias resolved via `ssh -G` to its real settings —
 * so shorthand tunnel commands use the actual IdentityFile, not a guess. */
export interface SshHostOut {
  alias: string;
  hostname: string | null;
  user: string | null;
  port: number;
  identity_files: string[];
  identity_file: string | null;
  /** true when a specific key is configured (vs. the default key fan-out). */
  identity_explicit: boolean;
  proxyjump: string | null;
}

/** A saved ssh command candidate for an endpoint's tunnel — lets a flaky
 * route (e.g. "skynet-alt") be swapped for a working one without touching
 * the endpoint's alias or base_url. */
export interface TunnelRouteOut {
  id: string;
  endpoint_id: string;
  label: string;
  command: string;
  local_port: number | null;
  active: boolean;
  created_ts: number | null;
}

export interface TunnelRouteTestResult {
  ok: boolean;
  latency_ms: number | null;
  error: string | null;
}

export interface TunnelTestResult {
  ssh_ok: boolean;
  ssh_latency_ms: number | null;
  endpoint_ok: boolean;
  endpoint_latency_ms: number | null;
  models: string[];
  error: string | null;
}

export interface RuntimeSettings {
  stream_passthrough: boolean;
  queue_requests: boolean;
  auto_failover: boolean;
  log_bodies: boolean;
  allow_cors: boolean;
  inject_stream_usage: boolean;
  proxy_port: number;
  retention_days: number;
  restart_required: boolean;
}

export interface ProxyInfoOut {
  base_url: string;
  port: number;
  example_model: string;
  uptime_s: number;
  requests_total: number;
  active_clients: number;
  db_size_bytes: number;
}

export interface RequestRow {
  id: string;
  ts: number;
  endpoint_id?: string | null;
  endpoint_name: string | null;
  route: string | null;
  model: string | null;
  stream: boolean;
  status: number | null;
  ok: boolean | null;
  state: 'streaming' | 'done' | 'error';
  error: string | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  total_tokens: number | null;
  ttft_ms: number | null;
  latency_ms: number | null;
  tokens_per_sec: number | null;
  cost_usd: number | null;
  temperature: number | null;
  max_tokens: number | null;
}

export interface RecentResponse {
  rows: RequestRow[];
  counts: { all: number; streaming: number; done: number; error: number };
}

export interface LiveSnapshot {
  in_flight: number;
  max_concurrency: number;
  /** At an upstream right now (<= max_concurrency). */
  active: number;
  /** Admitted but queued for a slot; non-zero means relay is the bottleneck. */
  waiting: number;
  /** Queue depth past which requests are shed with 429. */
  queue_limit: number;
  series: [number, number][];
}

/** ECharts dataset contract: dimensions header + source rows. */
export interface StatsDataset {
  dimensions: string[];
  source: (string | number | null)[][];
}

export interface SummaryOut {
  requests: number;
  errors: number;
  error_rate: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cost_usd: number;
  ttft_ms: { p50: number | null; p95: number | null };
  latency_ms: { p50: number | null; p95: number | null };
  tokens_per_sec: { p50: number | null };
}

export interface RouterState {
  policy: 'manual' | 'priority';
  pinned_id: string | null;
  resolved_id: string | null;
  resolved_name: string | null;
}

/** Dashboard time range selection. */
export type RangeId = '1h' | '24h' | '7d' | '30d' | '1y' | 'custom';
/** Tick granularity for the combined volume/tokens chart. */
export type DetailLevel = 'summary' | 'detailed';
export interface RangeSel {
  id: RangeId;
  /** unix seconds, only for custom */
  from?: number;
  to?: number;
}
