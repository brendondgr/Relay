// LLM Relay (relay) page inside GAIFA: Dashboard, Requests, Endpoints, Tunnels and Connect
// (design brief Part B2). Calls DashLLM's own admin API through ctx.call; its ECharts-shaped
// `{dimensions, source}` datasets are converted for the §11.6 chart components here.
import { chartCard, charts, rh } from '/mod-charts.js';
import { requireRunning, segmented, header, panel, columns, stat, store, fmtSecs, MONO } from '/mod-kit.js';

const WINDOWS = ['1h', '24h', '7d', '30d'];
const HEALTH_TONE = { healthy: 'green', degraded: 'accent', failed: 'red', unknown: 'dim' };
const errText = e => e.detail || e.message || 'Request failed';
const fmtN = v => (v == null ? '—' : v >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : v >= 1e4 ? `${(v / 1e3).toFixed(1)}k` : String(Math.round(v * 10) / 10));

export default {
  tabs: [
    { id: 'dashboard', label: 'Dashboard', icon: 'grid' }, { id: 'requests', label: 'Requests', icon: 'layers' },
    { id: 'endpoints', label: 'Endpoints', icon: 'relay' }, { id: 'tunnels', label: 'Tunnels', icon: 'ext' },
    { id: 'connect', label: 'Connect', icon: 'copy' },
  ],
  mount(el, ctx) {
    if (!requireRunning(el, ctx, 'the relay')) return;
    const cleanups = [];
    ({ requests: requestsTab, endpoints: endpointsTab, tunnels: tunnelsTab, connect: connectTab }[ctx.tab] || dashboardTab)(el, ctx, cleanups);
    return () => cleanups.forEach(f => { try { f(); } catch { /* gone */ } });
  },
};

/** `{dimensions, source}` → { col(name) → values[], labels[] } */
function ds(d) {
  const i = n => d.dimensions.indexOf(n);
  return { rows: d.source, col: n => d.source.map(r => r[i(n)]), first: d.source.map(r => r[0]) };
}
const toTs = label => {
  const m = /^(\d{4})-(\d{2})(?:-(\d{2}))?(?:T(\d{2}):(\d{2}))?$/.exec(label);
  return m ? new Date(+m[1], +m[2] - 1, +(m[3] || 1), +(m[4] || 0), +(m[5] || 0)).getTime() / 1000 : NaN;
};
const every = (fn, ms, cleanups) => { const t = setInterval(() => { if (!document.hidden) fn(); }, ms); cleanups.push(() => clearInterval(t)); };

// ------------------------------------------------------------------ Dashboard
function dashboardTab(el, ctx, cleanups) {
  const { ui } = ctx, h = ui.h;
  let win = store.get('relay.window', '24h');
  const q = () => ({ window: win });
  const kpis = h('div', { style: 'display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px' });
  const live = h('span', { style: `font-family:${MONO};font-size:12px;color:var(--mut,#8A94A6)` });
  const picker = segmented(WINDOWS.map(w => ({ value: w, label: w })), win, v => { win = v; store.set('relay.window', v); refresh(); }, 'Window');

  const tile = (label, value, sub, tone) => h('div', { style: 'background:var(--card,#11151D);border:1px solid var(--bd,#1D2430);border-radius:9px;padding:10px 12px;display:flex;flex-direction:column;gap:3px;min-width:0' },
    h('span', { style: 'font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--mut,#8A94A6)' }, label),
    h('span', { style: `font-family:${MONO};font-size:20px;font-weight:600;color:${tone || 'var(--tx,#E6EAF2)'}` }, value),
    sub ? h('span', { style: 'font-size:11px;color:var(--mut,#8A94A6)' }, sub) : null);

  async function loadKpis() {
    try {
      const s = (await ctx.call('GET', '/admin/stats/summary', { query: q() })).json;
      kpis.replaceChildren(
        tile('Requests', fmtN(s.requests), `${fmtN(s.errors)} errors`),
        tile('Tokens', fmtN(s.total_tokens), `${fmtN(s.prompt_tokens)} in · ${fmtN(s.completion_tokens)} out`),
        tile('TTFT p50 / p95', s.ttft_ms.p50 == null ? '—' : `${Math.round(s.ttft_ms.p50)} ms`, s.ttft_ms.p95 == null ? 'streamed requests only' : `p95 ${Math.round(s.ttft_ms.p95)} ms`),
        tile('Latency p50', s.latency_ms.p50 == null ? '—' : `${(s.latency_ms.p50 / 1000).toFixed(2)} s`, s.latency_ms.p95 == null ? '' : `p95 ${(s.latency_ms.p95 / 1000).toFixed(2)} s`),
        tile('Tokens / s', s.tokens_per_sec.p50 == null ? '—' : String(Math.round(s.tokens_per_sec.p50)), 'p50 per reply'),
        tile('Error rate', `${(s.error_rate * 100).toFixed(1)}%`, '', s.error_rate > 0.1 ? 'var(--rd,#F85149)' : null));
    } catch (e) { kpis.replaceChildren(ui.badge(errText(e), 'red')); }
  }
  async function loadLive() {
    try { const l = (await ctx.call('GET', '/admin/stats/live')).json; live.textContent = `${l.in_flight} in flight · ${l.active ?? 0} active · ${l.waiting ?? 0} waiting`; } catch { live.textContent = ''; }
  }

  const tokens = chartCard({
    title: 'Tokens over time', height: 170,
    load: () => ctx.call('GET', '/admin/stats/tokens/timeseries', { query: q() }).then(r => r.json),
    draw: (d, o) => { const x = ds(d); return rh(charts.AreaChart, { t: x.first.map(toTs), series: [{ label: 'Input', values: x.col('input') }, { label: 'Output', values: x.col('output') }], stacked: true, unit: 'count', ...o }); },
  });
  const models = chartCard({
    title: 'Requests by model', height: 170,
    load: () => ctx.call('GET', '/admin/stats/by-model', { query: q() }).then(r => r.json),
    draw: (d, o) => { const x = ds(d); return rh(charts.BarChart, { horizontal: true, categories: x.first.map(String), series: [{ label: 'Requests', values: x.col('requests') }, { label: 'Errors', values: x.col('errors') }], unit: 'count', ...o }); },
  });
  const latency = chartCard({
    title: 'Latency', height: 170,
    load: () => ctx.call('GET', '/admin/stats/latency', { query: q() }).then(r => r.json),
    draw: (d, o) => { const x = ds(d); const z = v => (v ? v : null); return rh(charts.LineChart, { t: x.first.map(toTs), series: [{ label: 'TTFT p50', values: x.col('ttft_p50').map(z) }, { label: 'TTFT p95', values: x.col('ttft_p95').map(z) }, { label: 'tok/s p50', values: x.col('tps_p50').map(z), axis: 'right' }], unit: 'ms', unitRight: 'count', ...o }); },
  });
  const byHour = chartCard({
    title: 'Tokens by hour of day', height: 170,
    load: () => ctx.call('GET', '/admin/stats/tokens/by-hour', { query: q() }).then(r => r.json),
    draw: (d, o) => { const x = ds(d); return rh(charts.BarChart, { categories: x.first.map(hh => `${String(hh).padStart(2, '0')}h`), series: [{ label: 'Input', values: x.col('input') }, { label: 'Output', values: x.col('output') }], stacked: true, unit: 'count', ...o }); },
  });
  const cards = [tokens, models, latency, byHour];
  cleanups.push(...cards.map(c => c.unmount));
  const refresh = () => { loadKpis(); cards.forEach(c => c.refresh()); };
  const grid = h('div', { style: 'display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:12px' }, ...cards);
  el.append(h('div', { style: 'display:flex;flex-direction:column;gap:12px;min-width:0' },
    h('div', { style: 'display:flex;gap:10px;align-items:center;flex-wrap:wrap' }, picker, h('span', { style: 'flex:1' }), live,
      ui.button('Native dashboard ↗', () => window.open(ctx.service.native_ui || 'http://127.0.0.1:4000/', '_blank'), { small: true, kind: 'ghost' })),
    kpis, grid));
  loadKpis(); loadLive();
  every(loadLive, 3000, cleanups);
  every(loadKpis, 30000, cleanups);
}

// ------------------------------------------------------------------ Requests
function requestsTab(el, ctx, cleanups) {
  const { ui } = ctx, h = ui.h;
  let only = 'all', paused = false, rows = [];
  const open = new Set();
  const body = h('tbody');
  const counts = h('span', { style: 'font-size:12px;color:var(--mut,#8A94A6)' });
  const filter = segmented([{ value: 'all', label: 'All' }, { value: 'ok', label: 'OK' }, { value: 'err', label: 'Errors' }], only, v => { only = v; render(); }, 'Filter');
  const pause = ui.button('Pause', () => { paused = !paused; pause.textContent = paused ? 'Resume' : 'Pause'; }, { small: true });
  const th = 'padding:6px 8px;text-align:left;font-weight:500;font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:var(--mut,#8A94A6);border-bottom:1px solid var(--bd,#1D2430);white-space:nowrap';
  const td = 'padding:6px 8px;border-bottom:1px solid var(--bd,#1D2430);font-size:12.5px;white-space:nowrap';
  function render() {
    const vis = rows.filter(r => only === 'all' || (only === 'ok' ? r.ok : !r.ok));
    body.replaceChildren(...vis.flatMap(r => {
      const tr = h('tr', { style: 'cursor:pointer', onclick: () => { open.has(r.id) ? open.delete(r.id) : open.add(r.id); render(); } },
        h('td', { style: td }, new Date(r.ts * 1000).toLocaleTimeString()),
        h('td', { style: td }, ui.badge(r.state === 'done' ? String(r.status) : r.state, r.ok ? 'green' : r.state === 'streaming' || r.state === 'pending' ? 'accent' : 'red')),
        h('td', { style: td + ';max-width:220px;overflow:hidden;text-overflow:ellipsis' }, r.model || '—'),
        h('td', { style: td }, r.endpoint_name || '—'),
        h('td', { style: td + `;font-family:${MONO};text-align:right` }, r.total_tokens == null ? '—' : `${r.prompt_tokens ?? 0} → ${r.completion_tokens ?? 0}`),
        h('td', { style: td + `;font-family:${MONO};text-align:right` }, r.latency_ms == null ? '—' : `${(r.latency_ms / 1000).toFixed(2)} s`),
        h('td', { style: td + `;font-family:${MONO};text-align:right` }, r.tokens_per_sec == null ? '—' : Math.round(r.tokens_per_sec)),
        h('td', { style: td }, r.stream ? 'stream' : ''));
      if (!open.has(r.id)) return [tr];
      return [tr, h('tr', {}, h('td', { colspan: 8, style: 'padding:8px;border-bottom:1px solid var(--bd,#1D2430)' },
        r.error ? h('pre', { style: `margin:0 0 8px;white-space:pre-wrap;color:var(--rd,#F85149);font-family:${MONO};font-size:11.5px` }, r.error) : null, ui.json(r),
        ui.text('Request and response bodies are kept only with "Log bodies" on in the relay\'s settings (native dashboard).', { muted: true, small: true })))];
    }));
  }
  async function load() {
    if (paused) return;
    try { const r = (await ctx.call('GET', '/admin/stats/recent', { query: { limit: 200 } })).json; rows = r.rows; counts.textContent = r.counts ? Object.entries(r.counts).map(([k, v]) => `${v} ${k}`).join(' · ') : `${rows.length} rows`; render(); } catch (e) { counts.textContent = errText(e); }
  }
  el.append(panel(header('Recent requests', counts, filter, pause),
    h('div', { style: 'overflow:auto;max-height:calc(100vh - 230px)' }, h('table', { style: 'width:100%;border-collapse:collapse' },
      h('thead', h('tr', ['Time', 'Status', 'Model', 'Endpoint', 'Tokens in → out', 'Latency', 'tok/s', ''].map(x => h('th', { style: th }, x)))), body))));
  load();
  every(load, 3000, cleanups);
}

// ------------------------------------------------------------------ Endpoints
function endpointsTab(el, ctx, cleanups) {
  const { ui } = ctx, h = ui.h;
  const list = h('div', { style: 'display:flex;flex-direction:column;gap:8px' });
  const routerInfo = h('span', { style: 'font-size:12px;color:var(--mut,#8A94A6)' });
  const act = (label, fn, kind) => ui.button(label, async e => { const b = e.currentTarget; b.disabled = true; try { await fn(); } catch (err) { ctx.toast(errText(err), 'error'); } finally { b.disabled = false; } }, { small: true, kind });
  async function load() {
    try {
      const [eps, rt] = await Promise.all([ctx.call('GET', '/admin/endpoints'), ctx.call('GET', '/admin/router')]);
      routerInfo.textContent = `routing: ${rt.json.policy}${rt.json.resolved_name ? ` → ${rt.json.resolved_name}` : ''}`;
      if (!eps.json.length) { list.replaceChildren(ui.empty('No endpoints', 'Add one below, or launch a model in the LLM Launcher.')); return; }
      list.replaceChildren(...eps.json.map(card));
    } catch (e) { list.replaceChildren(ui.badge(errText(e), 'red')); }
  }
  function card(ep) {
    const owned = ep.owner === 'lcpp';
    const head = h('div', { style: 'display:flex;gap:8px;align-items:center;flex-wrap:wrap' },
      h('strong', { style: 'font-size:13.5px' }, ep.name), ep.alias && ep.alias !== ep.name ? h('span', { style: `font-family:${MONO};font-size:11.5px;color:var(--mut,#8A94A6)` }, `alias ${ep.alias}`) : null,
      ui.badge(ep.health, HEALTH_TONE[ep.health] || 'dim'), ep.active ? ui.badge('active', 'accent') : null, !ep.enabled ? ui.badge('disabled', 'dim') : null,
      owned ? ui.badge('lcpp', 'accent') : null, h('span', { style: 'flex:1' }),
      h('span', { style: `font-family:${MONO};font-size:11.5px;color:var(--mut,#8A94A6)` }, `${ep.kind} · ${ep.protocol} · ${ep.ewma_latency_ms ? Math.round(ep.ewma_latency_ms) + ' ms' : '—'} · share ${Math.round((ep.share || 0) * 100)}%`));
    const buttons = owned
      ? [ui.text('Registered by the LLM Launcher; manage it there.', { muted: true, small: true }), act('Open Launcher', async () => ctx.navigate('#/s/lcpp'))]
      : [act('Test', async () => { const r = (await ctx.call('POST', `/admin/endpoints/${ep.id}/test`)).json; ctx.toast(r.ok === false ? `Test failed: ${r.error || r.detail || ''}` : `OK${r.latency_ms ? ` in ${Math.round(r.latency_ms)} ms` : ''}`, r.ok === false ? 'error' : 'success'); load(); }),
         ep.active ? null : act('Activate', async () => { await ctx.call('POST', `/admin/endpoints/${ep.id}/activate`); load(); }),
         act(ep.enabled ? 'Disable' : 'Enable', async () => { await ctx.call('PATCH', `/admin/endpoints/${ep.id}`, { json: { enabled: !ep.enabled } }); load(); }, 'ghost'),
         act('Delete', async () => { if (!await ctx.confirm({ title: `Delete endpoint “${ep.name}”?`, body: 'Clients using its alias stop reaching it. Its request history stays.', danger: true })) return; await ctx.call('DELETE', `/admin/endpoints/${ep.id}`); load(); }, 'danger')];
    return h('div', { style: 'border:1px solid var(--bd,#1D2430);border-radius:8px;padding:10px;background:var(--inset,#131926);display:flex;flex-direction:column;gap:6px;min-width:0' },
      head, h('span', { style: `font-family:${MONO};font-size:12px;word-break:break-all` }, ep.base_url),
      ep.available_models?.length ? h('span', { style: 'font-size:11.5px;color:var(--mut,#8A94A6)' }, `models: ${ep.available_models.join(', ')}`) : null,
      h('div', { style: 'display:flex;gap:6px;flex-wrap:wrap;align-items:center' }, buttons));
  }
  const field = (...a) => { const f = ui.field(...a); f.style.flex = '1 1 200px'; return f; };
  const name = ui.input({ placeholder: 'e.g. lab-box' }), url = ui.input({ placeholder: 'http://10.0.0.12:8080/v1' }), key = ui.input({ type: 'password', placeholder: 'optional' }), models = ui.input({ placeholder: 'optional, comma separated' });
  const add = ui.button('Add endpoint', async () => {
    if (!name.value.trim() || !url.value.trim()) return ctx.toast('A name and a base URL are needed', 'info');
    add.disabled = true;
    try {
      await ctx.call('POST', '/admin/endpoints', { json: { name: name.value.trim(), base_url: url.value.trim(), upstream_key: key.value || null, available_models: models.value.split(',').map(s => s.trim()).filter(Boolean) } });
      name.value = url.value = key.value = models.value = ''; ctx.toast('Endpoint added', 'success'); load();
    } catch (e) { ctx.toast(errText(e), 'error'); } finally { add.disabled = false; }
  }, { kind: 'primary' });
  const health = chartCard({
    title: 'Requests by endpoint · 24 h', height: 150,
    load: () => ctx.call('GET', '/admin/stats/by-endpoint', { query: { window: '24h' } }).then(r => r.json),
    draw: (d, o) => { const x = ds(d); return rh(charts.BarChart, { horizontal: true, categories: x.first.map(String), series: [{ label: 'Requests', values: x.col('requests') }, { label: 'Errors', values: x.col('errors') }], unit: 'count', ...o }); },
  });
  cleanups.push(health.unmount);
  const grid = columns(panel(header('Endpoints', routerInfo, ui.button('Refresh', load, { small: true, kind: 'ghost' })), list),
    h('div', { style: 'display:flex;flex-direction:column;gap:12px;min-width:0' }, panel(header('Add an endpoint'), h('div', { style: 'display:flex;gap:10px;flex-wrap:wrap' }, field('Name', name), field('Base URL', url), field('API key', key), field('Models', models)), h('div', {}, add)), health), 380);
  cleanups.push(grid.cleanup);
  el.append(grid);
  load();
  every(load, 15000, cleanups);
}

// ------------------------------------------------------------------ Tunnels
function tunnelsTab(el, ctx, cleanups) {
  const { ui } = ctx, h = ui.h;
  const list = h('div', { style: 'display:flex;flex-direction:column;gap:8px' });
  async function load() {
    try {
      const [eps, tunnels] = await Promise.all([ctx.call('GET', '/admin/endpoints'), ctx.call('GET', '/admin/tunnels')]);
      const tun = eps.json.filter(e => e.tunnel_command);
      const sessions = await Promise.all(tun.map(e => ctx.call('GET', `/admin/endpoints/${e.id}/tunnel`).then(r => r.json).catch(() => null)));
      const items = tun.map((e, i) => sessionCard(e, sessions[i])).concat(tunnels.json.map(tunnelCard));
      list.replaceChildren(...(items.length ? items : [ui.empty('No tunnels', 'Endpoints with an SSH tunnel command, and stand-alone tunnels, show here.')]));
    } catch (e) { list.replaceChildren(ui.badge(errText(e), 'red')); }
  }
  function sessionCard(ep, s) {
    const st = s?.status || 'unknown', base = `/admin/endpoints/${ep.id}/tunnel`;
    const answer = ui.input({ type: s?.prompt_secret ? 'password' : 'text', placeholder: s?.prompt_secret ? 'typed answers are not shown' : 'answer' });
    return h('div', { style: 'border:1px solid var(--bd,#1D2430);border-radius:8px;padding:10px;background:var(--inset,#131926);display:flex;flex-direction:column;gap:6px' },
      h('div', { style: 'display:flex;gap:8px;align-items:center;flex-wrap:wrap' }, h('strong', {}, ep.name), ui.badge(st, st === 'connected' ? 'green' : st === 'error' ? 'red' : st === 'prompt' ? 'accent' : 'dim'),
        ui.badge(`endpoint ${ep.health}`, HEALTH_TONE[ep.health] || 'dim'), h('span', { style: 'flex:1' }),
        s?.uptime_s ? h('span', { style: `font-family:${MONO};font-size:11.5px;color:var(--mut,#8A94A6)` }, `up ${fmtSecs(s.uptime_s)}`) : null),
      h('code', { style: `font-family:${MONO};font-size:11.5px;word-break:break-all` }, ep.tunnel_command),
      s?.last_error ? ui.badge(s.last_error, 'red') : null,
      s?.prompt ? h('div', { style: 'display:flex;gap:6px;align-items:center;flex-wrap:wrap' }, h('span', { style: 'font-size:12px' }, s.prompt), answer,
        ui.button('Send', async () => { try { await ctx.call('POST', `${base}/respond`, { json: { answer: answer.value } }); answer.value = ''; setTimeout(load, 500); } catch (e) { ctx.toast(errText(e), 'error'); } }, { small: true, kind: 'primary' })) : null,
      h('div', { style: 'display:flex;gap:6px' },
        st === 'connected' || st === 'connecting' || st === 'prompt'
          ? ui.button('Disconnect', async () => { await ctx.call('POST', `${base}/disconnect`).catch(e => ctx.toast(errText(e), 'error')); load(); }, { small: true })
          : ui.button('Connect', async () => { await ctx.call('POST', `${base}/connect`).catch(e => ctx.toast(errText(e), 'error')); setTimeout(load, 800); }, { small: true, kind: 'primary' })));
  }
  function tunnelCard(t) {
    return h('div', { style: 'border:1px solid var(--bd,#1D2430);border-radius:8px;padding:10px;background:var(--inset,#131926);display:flex;gap:8px;align-items:center;flex-wrap:wrap' },
      h('strong', {}, t.name || t.id), ui.badge(t.state || t.status || '—', (t.state || t.status) === 'running' ? 'green' : 'dim'), h('span', { style: 'flex:1' }),
      ['start', 'stop', 'test'].map(a => ui.button(a, async () => { try { await ctx.call('POST', `/admin/tunnels/${t.id}/${a}`); load(); } catch (e) { ctx.toast(errText(e), 'error'); } }, { small: true, kind: a === 'start' ? 'primary' : 'secondary' })));
  }
  el.append(panel(header('SSH tunnels', ui.button('Refresh', load, { small: true, kind: 'ghost' })),
    ui.text('Tunnel-backed endpoints connect on demand; when ssh asks something (a host key, a passphrase), answer it here.', { muted: true, small: true }), list));
  load();
  every(load, 5000, cleanups);
}

// ------------------------------------------------------------------ Connect
function connectTab(el, ctx, cleanups) {
  const { ui } = ctx, h = ui.h;
  const out = h('div', { style: 'display:flex;flex-direction:column;gap:12px;min-width:0' });
  const snippet = (title, code) => {
    const pre = h('pre', { style: `margin:0;background:var(--inset,#131926);border:1px solid var(--bd,#1D2430);border-radius:8px;padding:10px 12px;font-family:${MONO};font-size:12px;line-height:1.6;overflow:auto;white-space:pre` }, code);
    return panel(header(title, ui.button('Copy', () => navigator.clipboard?.writeText(code).then(() => ctx.toast('Copied', 'success'), () => ctx.toast('Copy failed', 'error')), { small: true })), pre);
  };
  Promise.all([ctx.call('GET', '/admin/proxy'), ctx.call('GET', '/v1/models')]).then(([p, m]) => {
    const base = p.json.base_url.replace('localhost', '127.0.0.1'), ids = m.json.data.map(x => x.id), model = p.json.example_model || 'auto';
    out.replaceChildren(
      panel(header('This relay'), h('div', { style: 'display:flex;gap:16px;flex-wrap:wrap' }, stat('Base URL', base), stat('API key', 'none (any value works)'), stat('Requests served', fmtN(p.json.requests_total)), stat('Database', `${(p.json.db_size_bytes / 1e6).toFixed(0)} MB`)),
        h('div', { style: 'display:flex;gap:6px;flex-wrap:wrap' }, ids.map(id => ui.badge(id, id === model ? 'accent' : 'dim')))),
      snippet('OpenAI SDK (Python)', `from openai import OpenAI\n\nclient = OpenAI(base_url="${base}", api_key="relay")\nreply = client.chat.completions.create(\n    model="${model}",\n    messages=[{"role": "user", "content": "Hello!"}],\n)\nprint(reply.choices[0].message.content)`),
      snippet('curl', `curl ${base}/chat/completions \\\n  -H 'content-type: application/json' \\\n  -d '{"model": "${model}", "messages": [{"role": "user", "content": "Hello!"}]}'`),
      snippet('Environment variables', `export OPENAI_BASE_URL=${base}\nexport OPENAI_API_KEY=relay\nexport OPENAI_MODEL=${model}`),
      snippet('JavaScript (fetch, streaming)', `const res = await fetch("${base}/chat/completions", {\n  method: "POST",\n  headers: { "content-type": "application/json" },\n  body: JSON.stringify({ model: "${model}", stream: true, messages: [{ role: "user", content: "Hello!" }] }),\n});`),
      ui.text('"auto" follows the relay\'s routing policy; any listed model id reaches that model directly; "agent" is the OpenCode agent server.', { muted: true, small: true }));
  }).catch(e => out.replaceChildren(ui.badge(errText(e), 'red')));
  el.append(out);
}
