/** Typed client for the relay API (see docs/api-contract.md).
 * Every call is logged; failures are logged as errors and rethrown. */

import { log } from './logger';
import type {
  DetailLevel,
  EndpointOut,
  EndpointTestResult,
  LiveSnapshot,
  ModelFactsOut,
  ModelFactsOverride,
  ProxyInfoOut,
  RangeSel,
  RecentResponse,
  RouterState,
  RuntimeSettings,
  SshHostOut,
  StatsDataset,
  SummaryOut,
  TunnelOut,
  TunnelRouteOut,
  TunnelRouteTestResult,
  TunnelSessionStatus,
  TunnelTestResult,
} from './types';

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  try {
    const res = await fetch(path, {
      method,
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) {
      const text = await res.text();
      log.error('api.fail', `${method} ${path} -> ${res.status} ${text.slice(0, 200)}`);
      throw new Error(`${method} ${path}: HTTP ${res.status}`);
    }
    if (res.status === 204) return undefined as T;
    return (await res.json()) as T;
  } catch (e) {
    if (!(e instanceof Error && e.message.startsWith(method))) {
      log.error('api.unreachable', `${method} ${path} — ${String(e)}`);
    }
    throw e;
  }
}

const get = <T>(path: string) => req<T>('GET', path);

/** window/custom range -> query string fragment */
export function rangeQuery(range: RangeSel): string {
  if (range.id === 'custom' && range.from != null && range.to != null) {
    return `from=${range.from}&to=${range.to}`;
  }
  return `window=${range.id}`;
}

export const api = {
  // ---- endpoints -----------------------------------------------------
  endpoints: () => get<EndpointOut[]>('/admin/endpoints'),
  createEndpoint: (body: {
    name: string; base_url: string; server_type: string;
    protocol?: 'openai' | 'opencode'; available_models?: string[];
    upstream_key?: string | null; priority?: number;
    alias?: string | null; model_override?: string | null;
    tunnel_command?: string | null; tunnel_local_port?: number | null;
    facts_override?: Partial<ModelFactsOverride> | null;
  }) => req<EndpointOut>('POST', '/admin/endpoints', body),
  patchEndpoint: (id: string, body: Record<string, unknown>) =>
    req<EndpointOut>('PATCH', `/admin/endpoints/${id}`, body),
  deleteEndpoint: (id: string) =>
    req<void>('DELETE', `/admin/endpoints/${id}`),
  activateEndpoint: (id: string) =>
    req<RouterState>('POST', `/admin/endpoints/${id}/activate`),
  testEndpoint: (id: string) =>
    req<EndpointTestResult>('POST', `/admin/endpoints/${id}/test`),
  endpointFacts: (id: string, refresh = false) =>
    get<ModelFactsOut>(`/admin/endpoints/${id}/facts${refresh ? '?refresh=1' : ''}`),

  // ---- ssh config hosts (~/.ssh/config, resolved via ssh -G) -----------
  sshHosts: () => get<SshHostOut[]>('/admin/ssh/hosts'),
  sshResolve: (host: string) =>
    get<SshHostOut>(`/admin/ssh/resolve?host=${encodeURIComponent(host)}`),

  // ---- interactive per-endpoint SSH tunnel sessions --------------------
  tunnelSessions: () =>
    get<TunnelSessionStatus[]>('/admin/endpoints/tunnel-sessions'),
  tunnelStatus: (id: string) =>
    get<TunnelSessionStatus>(`/admin/endpoints/${id}/tunnel`),
  connectEndpointTunnel: (id: string) =>
    req<TunnelSessionStatus>('POST', `/admin/endpoints/${id}/tunnel/connect`),
  disconnectEndpointTunnel: (id: string) =>
    req<TunnelSessionStatus>('POST', `/admin/endpoints/${id}/tunnel/disconnect`),
  respondEndpointTunnel: (id: string, text: string) =>
    req<TunnelSessionStatus>('POST', `/admin/endpoints/${id}/tunnel/respond`, { text }),

  // ---- saved ssh tunnel routes (multiple candidate commands) ----------
  tunnelRoutes: (eid: string) =>
    get<TunnelRouteOut[]>(`/admin/endpoints/${eid}/routes`),
  createTunnelRoute: (eid: string, body: { label: string; command: string }) =>
    req<TunnelRouteOut>('POST', `/admin/endpoints/${eid}/routes`, body),
  patchTunnelRoute: (eid: string, rid: string, body: Record<string, unknown>) =>
    req<TunnelRouteOut>('PATCH', `/admin/endpoints/${eid}/routes/${rid}`, body),
  deleteTunnelRoute: (eid: string, rid: string) =>
    req<void>('DELETE', `/admin/endpoints/${eid}/routes/${rid}`),
  activateTunnelRoute: (eid: string, rid: string) =>
    req<EndpointOut>('POST', `/admin/endpoints/${eid}/routes/${rid}/activate`),
  testTunnelRoute: (eid: string, rid: string) =>
    req<TunnelRouteTestResult>('POST', `/admin/endpoints/${eid}/routes/${rid}/test`),

  // ---- tunnels --------------------------------------------------------
  tunnels: () => get<TunnelOut[]>('/admin/tunnels'),
  createTunnel: (body: Record<string, unknown>) =>
    req<TunnelOut>('POST', '/admin/tunnels', body),
  patchTunnel: (id: string, body: Record<string, unknown>) =>
    req<TunnelOut>('PATCH', `/admin/tunnels/${id}`, body),
  deleteTunnel: (id: string) => req<void>('DELETE', `/admin/tunnels/${id}`),
  startTunnel: (id: string) => req<TunnelOut>('POST', `/admin/tunnels/${id}/start`),
  stopTunnel: (id: string) => req<TunnelOut>('POST', `/admin/tunnels/${id}/stop`),
  testTunnel: (id: string) =>
    req<TunnelTestResult>('POST', `/admin/tunnels/${id}/test`),
  tunnelCommand: (id: string) =>
    get<{ command: string }>(`/admin/tunnels/${id}/command`),

  // ---- interactive PTY session for a structured tunnel (SSH Tunnel tab) --
  tunnelSession: (id: string) =>
    get<TunnelSessionStatus>(`/admin/tunnels/${id}/session`),
  connectTunnelSession: (id: string) =>
    req<TunnelSessionStatus>('POST', `/admin/tunnels/${id}/session/connect`),
  disconnectTunnelSession: (id: string) =>
    req<TunnelSessionStatus>('POST', `/admin/tunnels/${id}/session/disconnect`),
  respondTunnelSession: (id: string, text: string) =>
    req<TunnelSessionStatus>('POST', `/admin/tunnels/${id}/session/respond`, { text }),

  // ---- settings / proxy -------------------------------------------------
  settings: () => get<RuntimeSettings>('/admin/settings'),
  updateSettings: (patch: Record<string, unknown>) =>
    req<RuntimeSettings>('PUT', '/admin/settings', patch),
  proxyInfo: () => get<ProxyInfoOut>('/admin/proxy'),

  // ---- stats ---------------------------------------------------------------
  summary: (r: RangeSel) => get<SummaryOut>(`/admin/stats/summary?${rangeQuery(r)}`),
  volume: (r: RangeSel, detail: DetailLevel = 'summary') =>
    get<StatsDataset>(`/admin/stats/volume?${rangeQuery(r)}&detail=${detail}`),
  tokensTimeseries: (r: RangeSel, detail: DetailLevel = 'summary') =>
    get<StatsDataset>(
      `/admin/stats/tokens/timeseries?${rangeQuery(r)}&detail=${detail}`),
  tokensByHour: (r: RangeSel) =>
    get<StatsDataset>(`/admin/stats/tokens/by-hour?${rangeQuery(r)}`),
  tokensByDay: (r: RangeSel) =>
    get<StatsDataset>(`/admin/stats/tokens/by-day?${rangeQuery(r)}`),
  byModel: (r: RangeSel) => get<StatsDataset>(`/admin/stats/by-model?${rangeQuery(r)}`),
  byEndpoint: (r: RangeSel) =>
    get<StatsDataset>(`/admin/stats/by-endpoint?${rangeQuery(r)}`),
  latency: (r: RangeSel) => get<StatsDataset>(`/admin/stats/latency?${rangeQuery(r)}`),
  recent: (limit = 90) => get<RecentResponse>(`/admin/stats/recent?limit=${limit}`),
  live: () => get<LiveSnapshot>('/admin/stats/live'),
};
