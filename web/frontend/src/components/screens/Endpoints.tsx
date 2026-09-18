import React, { useState, useCallback, useEffect } from 'react';
import type { CSSProperties } from 'react';
import type { EndpointOut, EndpointTestResult, SshHostOut, TunnelRouteOut, TunnelSessionStatus } from '../../lib/types';
import { C, ACCENT, MONO, SANS, dot, badge, statusColor } from '../../lib/styles';
import { api } from '../../lib/api';
import { log } from '../../lib/logger';
import { usePoll } from '../../hooks/usePoll';
import EndpointForm, {
  emptyForm, formFromEndpoint, inputStyle, parseModels, protocolFor, selectStyle,
} from './EndpointForm';
import type { EndpointFormValues } from './EndpointForm';

interface EndpointsProps {
  endpoints: EndpointOut[];
  swapping: string | null;
  onActivate: (id: string) => void;
  refresh: () => void;
}

type TestState = 'testing' | 'ok' | 'fail' | null;

interface TestEntry {
  state: TestState;
  result: EndpointTestResult | null;
}

/** The owner's UI, when its registration carried one (lcpp sends manager_url). */
const managerUrl = (e: EndpointOut): string | null => {
  const url = e.meta?.manager_url;
  return typeof url === 'string' && /^https?:\/\//.test(url) ? url : null;
};

/** Marks an endpoint another program registered (and will re-register). */
function ManagedBadge({ owner, managerUrl }: { owner: string; managerUrl: string | null }): React.JSX.Element {
  const title = `registered by ${owner} — it adds and removes this endpoint itself`;
  const label = `managed · ${owner}`;
  return managerUrl ? (
    <a href={managerUrl} target="_blank" rel="noopener" title={`${title}; open ${owner}`}
       style={{ ...badge(C.purple), textDecoration: 'none', whiteSpace: 'nowrap' }}>
      {label} ↗
    </a>
  ) : (
    <span style={{ ...badge(C.purple), whiteSpace: 'nowrap' }} title={title}>{label}</span>
  );
}

export default function Endpoints(props: EndpointsProps): React.JSX.Element {
  const { endpoints, swapping, onActivate, refresh } = props;

  const [addOpen, setAddOpen] = useState(false);
  const [form, setForm] = useState<EndpointFormValues>(emptyForm);
  const [tunnel, setTunnel] = useState('');
  const [tests, setTests] = useState<Record<string, TestEntry>>({});

  // ~/.ssh/config hosts, so a shorthand tunnel command can be built from a real
  // alias and we can show the actual IdentityFile ssh will use (not a guess).
  const [sshHosts, setSshHosts] = useState<SshHostOut[]>([]);
  const [tunnelHint, setTunnelHint] = useState<string | null>(null);
  useEffect(() => { api.sshHosts().then(setSshHosts).catch(() => { /* no config */ }); }, []);

  // Editing an existing endpoint inline (same form component as Add).
  const [editId, setEditId] = useState<string | null>(null);
  const [editForm, setEditForm] = useState<EndpointFormValues>(emptyForm);

  // Saved ssh routes (multiple candidate tunnel commands) per endpoint,
  // fetched lazily when a card's "SSH routes" panel is opened.
  const [routesOpenId, setRoutesOpenId] = useState<string | null>(null);
  const [routes, setRoutes] = useState<Record<string, TunnelRouteOut[]>>({});
  const [routeTests, setRouteTests] = useState<Record<string, TestState>>({});
  const [routeTestMsg, setRouteTestMsg] = useState<Record<string, string>>({});
  const [newRoute, setNewRoute] = useState({ label: '', command: '' });
  const [editRouteId, setEditRouteId] = useState<string | null>(null);
  const [editRouteForm, setEditRouteForm] = useState({ label: '', command: '' });

  // Live interactive tunnel-session state, keyed by endpoint id. Polled fast
  // while a prompt could be pending; nothing connects on its own.
  const { data: sessionList } = usePoll(() => api.tunnelSessions(), 1500);
  const sessions: Record<string, TunnelSessionStatus> = {};
  for (const s of sessionList ?? []) sessions[s.endpoint_id] = s;

  const [connecting, setConnecting] = useState<Record<string, boolean>>({});
  // Per-endpoint terminal input: typed lines / prompt answers sent to the PTY.
  const [termInput, setTermInput] = useState<Record<string, string>>({});

  const toggleAdd = useCallback(() => setAddOpen(o => !o), []);

  const patchForm = useCallback(
    (patch: Partial<EndpointFormValues>) => setForm(f => ({ ...f, ...patch })), []);

  const saveEp = useCallback(async () => {
    if (!form.name || !form.url) return;
    try {
      await api.createEndpoint({
        name: form.name,
        base_url: form.url,
        server_type: form.type,
        protocol: protocolFor(form.type),
        available_models: parseModels(form.models),
        upstream_key: form.key || null,
        alias: form.alias.trim() || null,
        tunnel_command: tunnel.trim() || null,
      });
      log.info('endpoints.create', `${form.name}${form.alias ? ` (alias ${form.alias})` : ''}${tunnel.trim() ? ' +tunnel' : ''}`);
      setAddOpen(false);
      setForm(emptyForm());
      setTunnel('');
      refresh();
    } catch {
      // api already logs errors
    }
  }, [form, tunnel, refresh]);

  // Build a shorthand `ssh -N -L ...` from a ~/.ssh/config alias, and surface
  // the real IdentityFile ssh resolves for it (or warn if none is configured).
  const pickTunnelHost = useCallback((alias: string) => {
    const h = sshHosts.find(x => x.alias === alias);
    if (!h) { setTunnelHint(null); return; }
    let port = 8000;
    try { const u = new URL(form.url); if (u.port) port = +u.port; } catch { /* ignore */ }
    setTunnel(`ssh -N -L 127.0.0.1:${port}:localhost:${port} ${alias}`);
    const via = h.proxyjump ? ` · via ${h.proxyjump}` : '';
    setTunnelHint(
      h.identity_explicit && h.identity_file
        ? `identity: ${h.identity_file} (from ~/.ssh/config)${via}`
        : `no specific key configured — ssh will try your default keys${via}`,
    );
  }, [sshHosts, form.url]);

  const connectTunnel = useCallback(async (id: string) => {
    setConnecting(c => ({ ...c, [id]: true }));
    log.info('endpoints.tunnel.connect', id);
    try {
      await api.connectEndpointTunnel(id);
    } catch {
      // api already logs errors
    } finally {
      setConnecting(c => ({ ...c, [id]: false }));
    }
  }, []);

  const disconnectTunnel = useCallback(async (id: string) => {
    log.info('endpoints.tunnel.disconnect', id);
    try {
      await api.disconnectEndpointTunnel(id);
    } catch {
      // api already logs errors
    }
  }, []);

  // Send a line straight into the endpoint's ssh PTY (answers a prompt or just
  // types into the terminal). Clears the per-endpoint input on success.
  const sendToTunnel = useCallback(async (id: string, secret: boolean) => {
    const text = termInput[id] ?? '';
    log.info('endpoints.tunnel.respond', `${id} secret=${secret}`);
    try {
      await api.respondEndpointTunnel(id, text);
      setTermInput(m => ({ ...m, [id]: '' }));
    } catch {
      // api already logs errors
    }
  }, [termInput]);

  const testEndpoint = useCallback(async (id: string) => {
    setTests(t => ({ ...t, [id]: { state: 'testing', result: null } }));
    log.info('endpoints.test', id);
    try {
      const result = await api.testEndpoint(id);
      setTests(t => ({ ...t, [id]: { state: result.ok ? 'ok' : 'fail', result } }));
      setTimeout(() => setTests(t => ({ ...t, [id]: { state: null, result: null } })), 3500);
    } catch {
      setTests(t => ({ ...t, [id]: { state: 'fail', result: null } }));
      setTimeout(() => setTests(t => ({ ...t, [id]: { state: null, result: null } })), 3500);
    }
  }, []);

  const startEdit = useCallback((e: EndpointOut) => {
    setEditId(e.id);
    setEditForm(formFromEndpoint(e));
  }, []);

  const cancelEdit = useCallback(() => setEditId(null), []);

  const patchEditForm = useCallback(
    (patch: Partial<EndpointFormValues>) => setEditForm(f => ({ ...f, ...patch })), []);

  const saveEdit = useCallback(async () => {
    if (!editId || !editForm.name || !editForm.url) return;
    const patch: Record<string, unknown> = {
      name: editForm.name,
      base_url: editForm.url,
      server_type: editForm.type,
      protocol: protocolFor(editForm.type),
      available_models: parseModels(editForm.models),
      alias: editForm.alias.trim() || null,
    };
    if (editForm.key.trim()) patch.upstream_key = editForm.key.trim();
    try {
      await api.patchEndpoint(editId, patch);
      log.info('endpoints.edit', editId);
      setEditId(null);
      refresh();
    } catch {
      // api already logs errors
    }
  }, [editId, editForm, refresh]);

  const loadRoutes = useCallback(async (eid: string) => {
    try {
      const list = await api.tunnelRoutes(eid);
      setRoutes(r => ({ ...r, [eid]: list }));
    } catch {
      // api already logs errors
    }
  }, []);

  const toggleRoutes = useCallback((eid: string) => {
    setRoutesOpenId(id => {
      const next = id === eid ? null : eid;
      if (next) loadRoutes(next);
      return next;
    });
    setNewRoute({ label: '', command: '' });
    setEditRouteId(null);
  }, [loadRoutes]);

  const addRoute = useCallback(async (eid: string) => {
    if (!newRoute.label.trim() || !newRoute.command.trim()) return;
    try {
      await api.createTunnelRoute(eid, { label: newRoute.label.trim(), command: newRoute.command.trim() });
      log.info('endpoints.route.create', `${eid} ${newRoute.label}`);
      setNewRoute({ label: '', command: '' });
      loadRoutes(eid);
    } catch {
      // api already logs errors
    }
  }, [newRoute, loadRoutes]);

  const startEditRoute = useCallback((r: TunnelRouteOut) => {
    setEditRouteId(r.id);
    setEditRouteForm({ label: r.label, command: r.command });
  }, []);

  const saveEditRoute = useCallback(async (eid: string) => {
    if (!editRouteId || !editRouteForm.label.trim() || !editRouteForm.command.trim()) return;
    try {
      await api.patchTunnelRoute(eid, editRouteId, {
        label: editRouteForm.label.trim(), command: editRouteForm.command.trim(),
      });
      log.info('endpoints.route.edit', editRouteId);
      setEditRouteId(null);
      loadRoutes(eid);
    } catch {
      // api already logs errors
    }
  }, [editRouteId, editRouteForm, loadRoutes]);

  const deleteRoute = useCallback(async (eid: string, rid: string) => {
    try {
      await api.deleteTunnelRoute(eid, rid);
      log.info('endpoints.route.delete', rid);
      loadRoutes(eid);
    } catch {
      // api already logs errors
    }
  }, [loadRoutes]);

  const activateRoute = useCallback(async (eid: string, rid: string) => {
    try {
      await api.activateTunnelRoute(eid, rid);
      log.info('endpoints.route.activate', rid);
      loadRoutes(eid);
      refresh();
    } catch {
      // api already logs errors
    }
  }, [loadRoutes, refresh]);

  const testRoute = useCallback(async (eid: string, rid: string) => {
    setRouteTests(t => ({ ...t, [rid]: 'testing' }));
    setRouteTestMsg(m => ({ ...m, [rid]: '' }));
    log.info('endpoints.route.test', rid);
    try {
      const result = await api.testTunnelRoute(eid, rid);
      setRouteTests(t => ({ ...t, [rid]: result.ok ? 'ok' : 'fail' }));
      setRouteTestMsg(m => ({ ...m, [rid]: result.ok ? `reachable · ${result.latency_ms}ms` : (result.error ?? 'unreachable') }));
    } catch {
      setRouteTests(t => ({ ...t, [rid]: 'fail' }));
      setRouteTestMsg(m => ({ ...m, [rid]: 'test failed' }));
    } finally {
      setTimeout(() => setRouteTests(t => ({ ...t, [rid]: null })), 5000);
    }
  }, []);

  return (
    <div style={{ padding: '14px 18px 24px', display: 'flex', flexDirection: 'column', gap: 12, maxWidth: 980 }}>
      {/* Top row */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <span style={{ font: `400 12px ${SANS}`, color: C.textMut }}>
          Requests route to the <b style={{ color: C.text }}>active</b> endpoint. Hot-swapping drains in-flight requests first.
        </span>
        <div
          onClick={toggleAdd}
          style={{
            padding: '6px 14px',
            borderRadius: 6,
            background: ACCENT,
            color: '#0B0E14',
            font: `600 12px ${SANS}`,
            cursor: 'pointer',
            userSelect: 'none',
          }}
        >
          {addOpen ? 'Close' : '+ Add endpoint'}
        </div>
      </div>

      {/* Add form */}
      {addOpen && (
        <div style={{
          background: C.bgCard,
          border: `1px solid ${C.borderStrong}`,
          borderRadius: 8,
          padding: 14,
          display: 'flex',
          flexDirection: 'column',
          gap: 10,
        }}>
          <span style={{ font: `600 11px ${SANS}`, letterSpacing: '.07em', textTransform: 'uppercase', color: C.textMut }}>
            New endpoint
          </span>
          <EndpointForm
            values={form}
            onChange={patchForm}
            onSave={saveEp}
            onCancel={toggleAdd}
            saveLabel="Save endpoint"
          >
            <input
              placeholder='ssh tunnel command (optional) — e.g. ssh -N -L 127.0.0.1:9090:localhost:9090 skynet-alt'
              value={tunnel}
              onChange={(e) => { setTunnel(e.target.value); setTunnelHint(null); }}
              style={inputStyle}
            />
            {sshHosts.length > 0 && (
              <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                <span style={{ font: `400 10.5px ${SANS}`, color: C.textDim, whiteSpace: 'nowrap' }}>
                  or build from ~/.ssh/config:
                </span>
                <select
                  defaultValue=""
                  onChange={(e) => { if (e.target.value) pickTunnelHost(e.target.value); }}
                  style={{ ...selectStyle, width: 'auto', minWidth: 180, cursor: 'pointer' }}
                >
                  <option value="">— pick a host —</option>
                  {sshHosts.map((h) => (
                    <option key={h.alias} value={h.alias}>
                      {h.alias}{h.hostname ? ` (${h.hostname})` : ''}{h.proxyjump ? ` ↝ ${h.proxyjump}` : ''}
                    </option>
                  ))}
                </select>
                {tunnelHint && (
                  <span style={{ font: `400 10px ${MONO}`, color: C.textDim, wordBreak: 'break-all' }}>
                    {tunnelHint}
                  </span>
                )}
              </div>
            )}
            <span style={{ font: `400 10.5px ${SANS}`, color: C.textDim }}>
              alias = routing name: clients send it as the <span style={{ fontFamily: MONO }}>model</span> to reach this server directly
              (e.g. <span style={{ fontFamily: MONO }}>"model": "skynet"</span>); relay rewrites it to the server's real model.
              Add an <b style={{ color: C.textMut }}>ssh tunnel command</b> for a remote server you reach over SSH — you connect it manually
              with <b style={{ color: C.textMut }}>Connect</b>, and any password/passphrase prompt appears here.
            </span>
          </EndpointForm>
        </div>
      )}

      {/* Endpoint cards */}
      {endpoints.map(e => {
        const testEntry = tests[e.id] ?? { state: null, result: null };
        const ts = testEntry.state;
        const tr = testEntry.result;

        const isSwapping = swapping === e.id;
        const locLabel = e.kind === 'local' ? 'local' : 'remote';
        const locColor = e.kind === 'local' ? C.green : C.purple;

        // Interactive tunnel state for this endpoint (only when it has a command).
        const hasTunnel = !!e.tunnel_command;
        const sess = sessions[e.id];
        const sStatus = sess?.status ?? 'idle';
        const tunnelUp = sStatus === 'up';
        const tunnelBusy = sStatus === 'connecting' || sStatus === 'awaiting_input' || connecting[e.id];
        const tunnelColor =
          tunnelUp ? C.green
          : sStatus === 'error' ? C.red
          : sStatus === 'awaiting_input' ? ACCENT
          : sStatus === 'connecting' ? C.cyan
          : C.textMut;
        const tunnelLabel =
          connecting[e.id] && sStatus === 'idle' ? '⟳ connecting…'
          : tunnelUp ? '● Disconnect'
          : sStatus === 'connecting' ? '⟳ connecting…'
          : sStatus === 'awaiting_input' ? '⌨ needs input'
          : sStatus === 'error' ? '↻ Reconnect'
          : 'Connect';
        const tunnelMsg =
          sStatus === 'up' && sess?.local_port ? `tunnel up · forwarding 127.0.0.1:${sess.local_port}`
          : sStatus === 'connecting' ? 'ssh connecting…'
          : sStatus === 'awaiting_input' ? `waiting: ${sess?.prompt ?? 'input required'}`
          : sStatus === 'error' ? (sess?.last_error ? `tunnel error: ${sess.last_error}` : 'tunnel error')
          : '';

        const latDisplay = (e.ewma_latency_ms != null && e.health !== 'failed')
          ? `${e.ewma_latency_ms}ms`
          : '—';

        const testLabel =
          ts === 'testing' ? '⟳ testing…'
          : ts === 'ok' ? '✓ OK'
          : ts === 'fail' ? '✕ failed'
          : 'Test connection';

        const testBorderColor =
          ts === 'ok' ? C.green
          : ts === 'fail' ? C.red
          : C.borderStrong;

        const testTextColor =
          ts === 'ok' ? C.green
          : ts === 'fail' ? C.red
          : C.textMut;

        const testStyle: CSSProperties = {
          padding: '6px 12px',
          borderRadius: 6,
          border: `1px solid ${testBorderColor}`,
          background: C.bgInset,
          color: testTextColor,
          font: `500 11.5px ${SANS}`,
          cursor: 'pointer',
          userSelect: 'none',
          whiteSpace: 'nowrap',
        };

        // What a probe actually calls depends on the protocol (see the
        // adapters), so the message has to follow it.
        const probePath = e.protocol === 'opencode' ? '/global/health' : '/models';
        const testMsg =
          ts === 'ok' && tr
            ? `GET ${e.base_url}${probePath} → 200 OK · ${tr.latency_ms}ms · ${tr.models[0] ?? ''}`
          : ts === 'fail' && tr?.error
            ? `GET ${e.base_url}${probePath} → ${tr.error}`
          : ts === 'fail'
            ? `GET ${e.base_url}${probePath} → connection refused (timeout 5s)`
          : ts === 'testing'
            ? `Probing ${probePath}…`
          : '';

        const testMsgColor =
          ts === 'fail' ? C.red
          : ts === 'ok' ? C.green
          : C.textMut;

        const cardStyle: CSSProperties = {
          display: 'flex',
          flexDirection: 'column',
          gap: 10,
          background: C.bgCard,
          border: `1px solid ${e.active ? 'rgba(255,178,36,.45)' : C.border}`,
          borderRadius: 8,
          padding: '14px 16px',
        };

        return (
          <div key={e.id} style={cardStyle}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
              <div style={dot(statusColor(e.health), e.health === 'healthy')} />
              <div style={{ display: 'flex', flexDirection: 'column', gap: 2, flex: 1, minWidth: 0 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span style={{ font: `600 13px ${MONO}` }}>{e.name}</span>
                  <span style={badge(C.cyan)}>{e.server_type}</span>
                  <span style={badge(locColor)}>{locLabel}</span>
                  {e.alias && (
                    <span style={badge(ACCENT)} title="model alias — send as &quot;model&quot; to route here">
                      model:{e.alias}
                    </span>
                  )}
                  {e.owner && (
                    <ManagedBadge owner={e.owner} managerUrl={managerUrl(e)} />
                  )}
                  {e.protocol !== 'openai' && (
                    <span style={badge(C.purple)} title="agent protocol — reachable only by name, never via &quot;auto&quot; or failover">
                      alias-only
                    </span>
                  )}
                  {e.active && (
                    <span style={{
                      padding: '2px 8px',
                      borderRadius: 4,
                      background: 'rgba(255,178,36,.14)',
                      border: '1px solid rgba(255,178,36,.4)',
                      color: ACCENT,
                      font: `700 9.5px ${SANS}`,
                      letterSpacing: '.08em',
                      textTransform: 'uppercase',
                    }}>
                      Active
                    </span>
                  )}
                </div>
                <span style={{ font: `400 11px ${MONO}`, color: C.textMut }}>
                  {e.base_url} <span style={{ color: C.textDim }}>·</span> {e.model ?? '—'}
                </span>
                {(e.available_models?.length ?? 0) > 0 && (
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5, marginTop: 2 }}>
                    {e.available_models.map(m => (
                      <span key={m} style={badge(C.cyan)} title="send this as &quot;model&quot; to reach this endpoint with this exact model">
                        {m}
                      </span>
                    ))}
                  </div>
                )}
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 1, marginRight: 6 }}>
                <span style={{ font: `500 12px ${MONO}`, color: C.text }}>{latDisplay}</span>
                <span style={{ font: `400 9.5px ${SANS}`, color: C.textDim }}>avg latency</span>
              </div>
              <div
                onClick={() => testEndpoint(e.id)}
                style={testStyle}
              >
                {testLabel}
              </div>
              <div
                onClick={() => (editId === e.id ? cancelEdit() : startEdit(e))}
                style={{
                  padding: '6px 12px',
                  borderRadius: 6,
                  border: `1px solid ${C.borderStrong}`,
                  background: editId === e.id ? 'rgba(255,178,36,.10)' : C.bgInset,
                  color: editId === e.id ? ACCENT : C.text,
                  font: `500 11.5px ${SANS}`,
                  cursor: 'pointer',
                  userSelect: 'none',
                  whiteSpace: 'nowrap',
                }}
              >
                {editId === e.id ? 'Cancel edit' : 'Edit'}
              </div>
              <div
                onClick={() => toggleRoutes(e.id)}
                style={{
                  padding: '6px 12px',
                  borderRadius: 6,
                  border: `1px solid ${C.borderStrong}`,
                  background: routesOpenId === e.id ? 'rgba(88,166,255,.10)' : C.bgInset,
                  color: routesOpenId === e.id ? C.cyan : C.text,
                  font: `500 11.5px ${SANS}`,
                  cursor: 'pointer',
                  userSelect: 'none',
                  whiteSpace: 'nowrap',
                }}
              >
                SSH routes{routes[e.id] ? ` (${routes[e.id].length})` : ''}
              </div>
              {hasTunnel && (
                <div
                  onClick={() => (tunnelUp ? disconnectTunnel(e.id) : connectTunnel(e.id))}
                  title={e.tunnel_command ?? undefined}
                  style={{
                    padding: '6px 12px',
                    borderRadius: 6,
                    border: `1px solid ${tunnelColor}`,
                    background: tunnelUp ? 'rgba(63,185,80,.10)' : C.bgInset,
                    color: tunnelColor,
                    font: `500 11.5px ${SANS}`,
                    cursor: tunnelBusy && !tunnelUp ? 'default' : 'pointer',
                    userSelect: 'none',
                    whiteSpace: 'nowrap',
                    opacity: tunnelBusy && !tunnelUp && sStatus !== 'awaiting_input' ? 0.75 : 1,
                  }}
                >
                  {tunnelLabel}
                </div>
              )}
              {!e.active && (
                <div
                  onClick={() => onActivate(e.id)}
                  style={{
                    padding: '6px 12px',
                    borderRadius: 6,
                    border: `1px solid ${C.borderStrong}`,
                    background: C.bgInset,
                    color: C.text,
                    font: `500 11.5px ${SANS}`,
                    cursor: 'pointer',
                    userSelect: 'none',
                    whiteSpace: 'nowrap',
                  }}
                >
                  {isSwapping ? 'draining…' : 'Set active'}
                </div>
              )}
            </div>
            {testMsg && (
              <div style={{
                font: `400 11px ${MONO}`,
                color: testMsgColor,
                background: C.bg,
                border: `1px solid ${C.border}`,
                borderRadius: 6,
                padding: '8px 12px',
              }}>
                {testMsg}
              </div>
            )}
            {/* Interactive terminal: live ssh output for this endpoint's tunnel.
                Appears once Connect starts a session; a password/passphrase or
                host-key prompt is answered right here in the input line. */}
            {sess && (sStatus === 'connecting' || sStatus === 'awaiting_input'
                || sStatus === 'up' || sStatus === 'error'
                || (sess.output?.length ?? 0) > 0) && (() => {
              const live = sStatus === 'connecting' || sStatus === 'awaiting_input' || sStatus === 'up';
              const secret = !!sess.prompt_secret;
              return (
                <div style={{
                  display: 'flex', flexDirection: 'column', gap: 8,
                  background: C.bg, border: `1px solid ${C.border}`, borderRadius: 6, padding: 12,
                }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <div style={{
                      width: 7, height: 7, borderRadius: '50%', background: tunnelColor,
                      animation: live ? 'livepulse 2s infinite' : 'none',
                    }} />
                    <span style={{ font: `600 10px ${SANS}`, letterSpacing: '.06em', textTransform: 'uppercase', color: C.textMut }}>
                      Terminal
                    </span>
                    {tunnelMsg && (
                      <span style={{ font: `400 10.5px ${MONO}`, color: tunnelColor, marginLeft: 'auto' }}>
                        {tunnelMsg}
                      </span>
                    )}
                  </div>
                  <div
                    ref={(el) => { if (el) el.scrollTop = el.scrollHeight; }}
                    style={{
                      background: '#05070B', border: `1px solid ${C.border}`, borderRadius: 6,
                      padding: '10px 12px', maxHeight: 220, overflowY: 'auto',
                      font: `400 11.5px/1.55 ${MONO}`, color: C.text,
                      whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                    }}
                  >
                    {(sess.output ?? []).length === 0
                      ? <span style={{ color: C.textDim }}>ssh starting…</span>
                      : (sess.output ?? []).map((ln, i) => <div key={i}>{ln}</div>)}
                  </div>
                  <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                    <span style={{ font: `600 13px ${MONO}`, color: sStatus === 'awaiting_input' ? ACCENT : C.textDim }}>
                      {sStatus === 'awaiting_input' ? '?' : '›'}
                    </span>
                    <input
                      type={secret ? 'password' : 'text'}
                      value={termInput[e.id] ?? ''}
                      disabled={!live}
                      placeholder={
                        !live ? 'session not connected'
                        : sStatus === 'awaiting_input' ? (sess.prompt ?? 'ssh is asking for input')
                        : 'send a line to the terminal (e.g. yes)'
                      }
                      onChange={(ev) => { const v = ev.target.value; setTermInput(m => ({ ...m, [e.id]: v })); }}
                      onKeyDown={(ev) => { if (ev.key === 'Enter') void sendToTunnel(e.id, secret); }}
                      style={{ ...inputStyle, flex: 1, opacity: live ? 1 : 0.6 }}
                    />
                    <div
                      onClick={() => { if (live) void sendToTunnel(e.id, secret); }}
                      style={{
                        padding: '7px 14px', borderRadius: 6,
                        background: live ? ACCENT : C.bgInset,
                        border: live ? 'none' : `1px solid ${C.borderStrong}`,
                        color: live ? '#0B0E14' : C.textDim,
                        font: `600 11px ${SANS}`, cursor: live ? 'pointer' : 'default',
                        userSelect: 'none', whiteSpace: 'nowrap',
                      }}
                    >
                      Send
                    </div>
                  </div>
                  {secret && sStatus === 'awaiting_input' && (
                    <span style={{ font: `400 10px ${SANS}`, color: C.textDim }}>
                      Sent straight to ssh over the local PTY; never stored or logged.
                    </span>
                  )}
                </div>
              );
            })()}

            {/* Edit endpoint */}
            {editId === e.id && (
              <div style={{
                display: 'flex', flexDirection: 'column', gap: 10,
                background: C.bg, border: `1px solid ${C.border}`, borderRadius: 6, padding: 12,
              }}>
                {e.owner && (
                  <div style={{ font: `400 11.5px ${SANS}`, color: C.textMut }}>
                    Registered by <span style={{ color: C.purple }}>{e.owner}</span>
                    {e.external_key && <> as <span style={{ font: `400 11px ${MONO}` }}>{e.external_key}</span></>}.
                    {' '}Its name, URL, alias and model list are set again whenever {e.owner} re-registers it;
                    manage it from {managerUrl(e)
                      ? <a href={managerUrl(e)!} target="_blank" rel="noopener" style={{ color: C.cyan }}>{e.owner}</a>
                      : e.owner} instead.
                  </div>
                )}
                <EndpointForm
                  values={editForm}
                  onChange={patchEditForm}
                  onSave={saveEdit}
                  onCancel={cancelEdit}
                  saveLabel="Save changes"
                  keepKeyHint
                />
              </div>
            )}

            {/* SSH routes: multiple candidate tunnel commands for this endpoint. */}
            {routesOpenId === e.id && (
              <div style={{
                display: 'flex', flexDirection: 'column', gap: 8,
                background: C.bg, border: `1px solid ${C.border}`, borderRadius: 6, padding: 12,
              }}>
                <span style={{ font: `400 10.5px ${SANS}`, color: C.textDim }}>
                  Save more than one ssh command for this endpoint (e.g. two hosts that reach the same server).
                  <b style={{ color: C.textMut }}> Test</b> checks reachability without opening a tunnel;
                  <b style={{ color: C.textMut }}> Use</b> makes it the command <b style={{ color: C.textMut }}>Connect</b> runs.
                </span>
                {(routes[e.id] ?? []).map(r => {
                  const rts = routeTests[r.id] ?? null;
                  const rMsg = routeTestMsg[r.id] ?? '';
                  const rColor = rts === 'ok' ? C.green : rts === 'fail' ? C.red : C.textMut;
                  if (editRouteId === r.id) {
                    return (
                      <div key={r.id} style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                        <input value={editRouteForm.label}
                          onChange={(ev) => setEditRouteForm(f => ({ ...f, label: ev.target.value }))}
                          style={{ ...inputStyle, width: 110 }} placeholder="label" />
                        <input value={editRouteForm.command}
                          onChange={(ev) => setEditRouteForm(f => ({ ...f, command: ev.target.value }))}
                          style={{ ...inputStyle, flex: 1 }} placeholder="ssh -N -L ..." />
                        <div onClick={() => saveEditRoute(e.id)} style={{
                          padding: '5px 10px', borderRadius: 6, background: '#3FB950',
                          color: '#0B0E14', font: `600 11px ${SANS}`, cursor: 'pointer', whiteSpace: 'nowrap',
                        }}>Save</div>
                        <div onClick={() => setEditRouteId(null)} style={{
                          padding: '5px 10px', borderRadius: 6, border: `1px solid ${C.borderStrong}`,
                          color: C.textMut, font: `500 11px ${SANS}`, cursor: 'pointer', whiteSpace: 'nowrap',
                        }}>Cancel</div>
                      </div>
                    );
                  }
                  return (
                    <div key={r.id} style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                        <span style={{ font: `600 11px ${SANS}`, color: C.text, minWidth: 70 }}>{r.label}</span>
                        {r.active && <span style={badge(ACCENT)}>in use</span>}
                        <span title={r.command} style={{
                          font: `400 11px ${MONO}`, color: C.textMut, flex: 1, minWidth: 0,
                          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                        }}>{r.command}</span>
                        <div onClick={() => testRoute(e.id, r.id)} style={{
                          padding: '4px 10px', borderRadius: 6, border: `1px solid ${rColor}`,
                          color: rColor, font: `500 11px ${SANS}`, cursor: 'pointer', whiteSpace: 'nowrap',
                        }}>{rts === 'testing' ? '⟳ testing…' : rts === 'ok' ? '✓ reachable' : rts === 'fail' ? '✕ failed' : 'Test'}</div>
                        {!r.active && (
                          <div onClick={() => activateRoute(e.id, r.id)} style={{
                            padding: '4px 10px', borderRadius: 6, border: `1px solid ${C.borderStrong}`,
                            color: C.text, font: `500 11px ${SANS}`, cursor: 'pointer', whiteSpace: 'nowrap',
                          }}>Use</div>
                        )}
                        <div onClick={() => startEditRoute(r)} style={{
                          padding: '4px 10px', borderRadius: 6, border: `1px solid ${C.borderStrong}`,
                          color: C.textMut, font: `500 11px ${SANS}`, cursor: 'pointer', whiteSpace: 'nowrap',
                        }}>Edit</div>
                        <div onClick={() => deleteRoute(e.id, r.id)} style={{
                          padding: '4px 10px', borderRadius: 6, border: `1px solid ${C.borderStrong}`,
                          color: C.red, font: `500 11px ${SANS}`, cursor: 'pointer', whiteSpace: 'nowrap',
                        }}>Delete</div>
                      </div>
                      {rMsg && <span style={{ font: `400 10.5px ${MONO}`, color: rColor, paddingLeft: 78 }}>{rMsg}</span>}
                    </div>
                  );
                })}
                {(routes[e.id]?.length ?? 0) === 0 && (
                  <span style={{ font: `400 11px ${SANS}`, color: C.textDim }}>No saved routes yet.</span>
                )}
                <div style={{ display: 'flex', gap: 8, alignItems: 'center', paddingTop: 4, borderTop: `1px solid ${C.border}` }}>
                  <input placeholder="label (e.g. skynet-alt)" value={newRoute.label}
                    onChange={(ev) => setNewRoute(f => ({ ...f, label: ev.target.value }))}
                    style={{ ...inputStyle, width: 150 }} />
                  <input placeholder="ssh -N -L 127.0.0.1:9090:localhost:9090 skynet-alt" value={newRoute.command}
                    onChange={(ev) => setNewRoute(f => ({ ...f, command: ev.target.value }))}
                    style={{ ...inputStyle, flex: 1 }} />
                  <div onClick={() => addRoute(e.id)} style={{
                    padding: '6px 12px', borderRadius: 6, background: ACCENT,
                    color: '#0B0E14', font: `600 11.5px ${SANS}`, cursor: 'pointer', whiteSpace: 'nowrap',
                  }}>+ Add route</div>
                </div>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
