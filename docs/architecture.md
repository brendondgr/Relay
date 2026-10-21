# Architecture

## Shape

The backend is a FastAPI (ASGI, async) service; the frontend is an Astro site
with a single React island. In production FastAPI serves the built
`web/frontend/dist` statically from `/`, so the whole product is one process
on one port (default `:4000`). When `dist` is absent the backend logs
`frontend dist not found; API-only mode` and starts anyway.

## Surfaces

1. **`/v1/*`** — OpenAI-compatible endpoint the *world* calls. A single
   catch-all route (`app/routes/v1.py`) hands everything to the proxy
   service, which classifies the path itself.
2. **`/admin/endpoints/*`, `/admin/tunnels/*`, `/admin/ssh/*`,
   `/admin/settings`, `/admin/router`, `/admin/proxy`, `/admin/logs/*`** —
   control plane the *dashboard* calls.
3. **`/admin/stats/*`** — read API the dashboard polls; returns ECharts
   `dataset`-friendly payloads (`dimensions` + `source`).

Plus `GET /health` for liveness (`{status, version, uptime_s}`).

## Structural idea

Requests resolve their upstream target **at call time from live router
state**. Hot-swapping an endpoint or failing one over is just mutating that
state; in-flight requests are untouched and the next request picks up the
change with no restart.

Resolution order per request (`proxy._dispatch` → `router.resolve`):

1. **Model alias** — if the request's `model` matches an endpoint's `alias`
   (e.g. `"model": "skynet"`), the request is pinned to that endpoint and
   `model` is rewritten to what that server actually runs (`model_override`,
   else the prober-discovered default). Alias-pinned requests do **not**
   fail over — the caller named the server, so errors surface instead.
2. **Manual pin** — under `policy: "manual"` the dashboard's active endpoint
   (the hot-swap control) wins while it is enabled and not `failed`. If it
   *is* failed and `auto_failover` is off, the request is still sent there so
   the error is visible rather than silently rerouted.
3. **Priority failover** — otherwise the enabled, non-`failed` endpoint with
   the highest `priority` wins (ties broken by `created_ts`). Up to 3 attempts
   per request, each excluding endpoints already tried.

`model: "auto"` is the advertised default and is never sent upstream: it is
rewritten to the resolved endpoint's real model **per attempt**, because
failover may land on an endpoint running a different model.

```
                        ┌─────────────────────────────────────────────┐
  OpenAI SDK / curl ──▶ │  /v1/chat/completions  /v1/embeddings  ...  │
                        │                 PROXY LAYER                 │
                        │   • no auth: open front door                │
                        │   • resolve target via ROUTER (live state)  │
                        │   • rewrite model (alias / auto / override)  │
                        │   • stream tee: capture TTFT + usage        │
                        │   • record telemetry off the hot path       │
                        └───────┬───────────────────────────┬─────────┘
                                │                           │
                   ┌────────────▼─────────┐       ┌─────────▼──────────┐
                   │       ROUTER         │       │  TELEMETRY WRITER  │
                   │ endpoint registry +  │       │  async queue →     │
                   │ health state + policy│       │  requests + hourly │
                   └───┬──────────┬───────┘       │  rollups           │
       health probes   │          │ passive       └─────────┬──────────┘
       (background)    │          │ failures                │
             ┌─────────▼──┐   ┌───▼─────────┐      ┌────────▼───────┐
             │ endpoint A │   │ endpoint B  │      │   STATS API    │◀── dashboard
             │  (local)   │   │ (ssh tunnel)│      │ aggregates +   │    polls
             └────────────┘   └──────┬──────┘      │ ECharts shapes │
                                     │             └────────────────┘
                         ┌───────────▼──────────┐
                         │  SSH TUNNEL SESSION  │
                         │  PTY-backed ssh -N   │
                         └──────────────────────┘
```

## Subsystems

| Subsystem | Module | Notes |
| --- | --- | --- |
| App wiring / lifespan | `app/main.py` | boots telemetry writer, health prober, tunnel supervisor, housekeeping; dynamic CORS middleware; serves frontend dist |
| Config | `app/config.py` | pydantic-settings; `RELAY_*` and `OPENCODE_*` env vars, repo-root `.env`; boot-time values only |
| Runtime settings | `app/services/settings_store.py` | DB-backed mutable toggles |
| Logging | `app/core/logging.py` | rotating file + console, JSON lines |
| Client labeling | `app/security.py` | bearer extraction + masking for telemetry attribution. No auth plane — nothing here rejects a request |
| OpenCode bootstrap | `app/services/opencode_boot.py` | upserts the agent endpoint at boot, then discovers and publishes its free models |
| Storage | `app/db.py` | SQLite (WAL), additive column migrations, one-time rollup backfill |
| Proxy | `app/services/proxy.py` (with `admission.py`, the bounded gate at the door, and `sse.py`, usage and text read back from a stream) + `app/routes/v1.py` | streaming tee, TTFT, usage capture, pre-first-byte retry, stale-override self-heal |
| Protocol adapters | `app/services/adapters/` | per-`protocol` probe + forward; `passthrough` (OpenAI) and `opencode` (agent sessions) |
| Model catalog | `app/services/catalog.py` | the synthesized `GET /v1/models`, with `meta` on every entry |
| Model facts | `app/services/model_facts.py` | context window / tools / vision / reasoning per endpoint+model: override, llama.cpp `/props`, vLLM `max_model_len`, or a probe; cached, read in the background |
| Router | `app/services/router.py` | registry, alias + allowlist routing, health state machine, tunnel routes |
| Health | `app/services/health.py` | probe cadence + state feed; the probe request itself comes from the adapter |
| Stats | `app/services/stats.py` + `app/routes/admin_stats.py` | ECharts-shaped aggregates over rollups and raw rows |
| Rollups | `app/services/rollup.py`, `app/services/histogram.py` | hourly pre-aggregation + log-spaced percentile histograms |
| Telemetry | `app/services/telemetry.py` | asyncio queue, off-hot-path writes, `LiveTracker`, retention pruning |
| Structured tunnels | `app/services/tunnels.py` + `app/routes/admin_tunnels.py` | argv `ssh` children, supervisor with backoff, two-level test |
| Interactive tunnels | `app/services/tunnel_sessions.py` | PTY-backed `ssh`, one session per endpoint, prompt/respond |
| SSH config | `app/services/ssh_config.py` | reads `~/.ssh/config` via `ssh -G` to resolve real host settings |
| Frontend shell | `web/frontend/src/components/App.tsx` | sidebar, header, hot-swap, screen switch |
| Screens | `web/frontend/src/components/screens/*.tsx` | one module per screen |
| Charts | `web/frontend/src/components/EChart.tsx`, `src/lib/chartOptions.ts` | echarts/core, canvas renderer |
| API client | `web/frontend/src/lib/api.ts`, `src/hooks/usePoll.ts` | typed fetchers + polling |

## Health-state machine

```
        probe ok / request ok
   ┌───────────────◀───────────────┐
HEALTHY ──fails ≥ N──▶ DEGRADED ──fails keep coming──▶ FAILED (out of pool)
   ▲                                 (only while probes                │
   │                                  are failing too)                 │
   └────────── M consecutive probe successes ◀────────────┘
```

Active probes run every `RELAY_PROBE_INTERVAL` (default 15s), and real
request outcomes feed the same signals. `N` is `RELAY_UNHEALTHY_AFTER`
(default 3) and `M` is `RELAY_RECOVER_AFTER` (default 2).

Both edges touching `FAILED` are probe-gated, and symmetrically so.

Recovery out of `FAILED` requires **probe** successes specifically — a
successful request alone does not clear it, which avoids flapping when one
lucky request slips through a mostly-dead endpoint. Entry into `FAILED`
likewise needs the prober's agreement: while the last probe passed, a run of
failed *requests* takes the endpoint only as far as `DEGRADED`, so it stays
in the pool. Under congestion an overloaded but perfectly alive server
returns timeouts, and treating those as death removed the only endpoint and
turned a busy relay into a relay that answered nothing but
`no healthy upstream`. A server that is really gone fails its probes too, and
is out within one `RELAY_PROBE_INTERVAL`.

Success also feeds an EWMA latency (`0.3 * new + 0.7 * old`).

## Streaming and failover

Transparent retry on another endpoint is safe **only before the first byte**
reaches the client, so it only happens on connect errors and 5xx responses
that arrive before the tee starts. After the first byte, errors surface to the
client; there is no mid-stream replay.

## The protocol seam

Relay's hot path is OpenAI-shaped end to end, which is correct for every
llama.cpp / vLLM / ollama box and wrong for an agent server. Rather than
branch through `proxy.py`, an endpoint's `protocol` column selects an adapter
(`app/services/adapters/`) exposing exactly two methods:

- `probe(http, endpoint, timeout)` — what a health check *is*. `/models` for
  OpenAI, `/global/health` + `/config/providers` for OpenCode. `health.py`
  keeps the cadence and the state machine; only the request moved.
- `forward(proxy, request, endpoint, path, record, body, t0)` — one
  attempt, returning an OpenAI-shaped response.

The interface is deliberately narrow. Everything *above* a single attempt —
the concurrency gate, alias pinning, the failover loop, and the telemetry
call sites (`_buffered_response`, `_stream_response`,
`finalize_adapter_response`) — stays in `ProxyService` and is shared. An
adapter that owned `handle()` would fork all of it, and the dashboard would
start showing different columns depending on which upstream answered.

Two consequences worth remembering:

- **Adding a protocol means touching `health.py` too** — or rather, means
  *not* touching it, as long as the new adapter implements `probe`.
- **Non-`openai` protocols are alias-only.** `Router._eligible`, the manual
  pin in `resolve()`, and the first-endpoint auto-pin in `create()` all
  exclude them, so an agent server is only ever reached by a caller that
  named it. Alias-pinned requests already never fail over, which is the
  behavior we want and comes free.

`services/adapters/passthrough.py` is a verbatim extraction of the original
code paths — the refactor that introduced this seam changed no behavior and
left all 91 prior tests untouched.

## Stale `model_override` self-healing

If an endpoint pins `model_override` and the model on that port is later
swapped out, the upstream rejects the request with an unknown-model error.
The proxy detects that (a 4xx/5xx body naming the model plus a known marker
phrase), clears the override in memory and in the DB, and retries the **same**
endpoint with the prober-discovered model. Alias pinning still holds — it
never switches endpoints. If no replacement model was discovered, the
rejection goes through the normal failure path instead of resending a dead
model id.

## Model facts

An agent needs a model's context window, and whether it calls tools, takes
images or reasons, before it sends anything. `GET /v1/models` adds a `meta`
object to every entry (`context_length`, `tools`, `vision`, `reasoning`,
`source`; null = unknown), read per endpoint and upstream model:

1. `facts_override` on the endpoint (Endpoints page) wins, field by field.
2. llama.cpp: `GET <origin>/props` (origin = base URL without `/v1`):
   `default_generation_settings.n_ctx`, `modalities.vision`,
   `chat_template_caps.supports_tools` (older builds: "tool" in the template),
   and reasoning when the chat template has a thinking switch.
3. vLLM: `max_model_len` on the model's entry in `<base>/models`.
4. An upstream that is itself a relay (its catalog has `relay` objects on its
   entries; also any `server_type: vLLM`): one chat request with
   `max_tokens: 10000000`, `stream: true`. vLLM's 400 names the limit
   (`max_model_len=…=131072`, or "maximum context length is N"); a 2xx is
   dropped at once, so nothing generates for long. Plain OpenAI-compatible
   servers are never probed.

`source` says where `context_length` came from (`override`, `props`, `vllm`,
`probe`), or `override` when only overridden fields are known. Protocols other
than `openai` are never read: their facts are the override's.

`/v1/models` must answer fast with an upstream slow or gone, so a lookup
starts the reads that are due, waits up to 3 s, and answers with what is known;
the rest fill the cache in the background (reads use the probe pool). The
cache key is (endpoint, base URL, upstream model), so a changed URL or model
is a fresh read. A failed read is retried after 5 minutes, a good one re-read
after an hour (the old value is served meanwhile).
`GET /admin/endpoints/{id}/facts?refresh=1` reads again now.

## Storage and scale

Raw telemetry lands in `requests` (one row per proxied request; captured
bodies go to `request_bodies` as zlib-compressed BLOBs, only when `log_bodies`
is on). The writer *also* accumulates hourly rollups:

- `request_rollup_hourly` — additive counts/sums keyed by
  `(bucket_hour, endpoint_id, model)`, with `endpoint_name` denormalized so
  history survives an endpoint being deleted.
- `request_rollup_hist` — per-hour, per-metric log-spaced histogram buckets
  for `ttft`, `latency`, and `tps`.

Percentiles cannot be summed across buckets, which is why the histograms
exist: bucket counts *are* additive, so a wide-window percentile is a
`SUM(count) GROUP BY bucket_idx`. Consequently percentiles are **hybrid** —
exact from raw rows for spans up to ~24h, approximate from histograms beyond.
Hour-aligned windows read rollups (O(hours)); only the minute-bucketed `1h`
window scans raw rows.

The histogram bucket edges in `services/histogram.py` are an **on-disk format
version**: changing `METRICS` invalidates every stored `bucket_idx`.

## Frontend/backend boundary

The frontend owns rendering and interaction only; all state (endpoints,
health, tunnels, settings, telemetry) lives in the backend. The frontend polls
read endpoints and issues commands via the control plane. UI events are
shipped to `POST /admin/logs/frontend`, so client-side actions appear in the
server log stream and in the `frontend_logs` table.
