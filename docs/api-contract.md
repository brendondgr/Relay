# API Contract

Shared contract between `web/backend` and `web/frontend`. The authoritative
source is `web/backend/app/schemas/__init__.py`; the frontend's typed client
(`web/frontend/src/lib/types.ts`) mirrors it.

## Conventions

- All admin responses are JSON. Errors: `{"detail": "..."}` with proper HTTP
  status codes. `/v1` proxy errors instead use the OpenAI error envelope:
  `{"error": {"message", "type": "relay_proxy_error", "code"}}`.
- Stats endpoints return the ECharts `dataset` contract:
  `{"dimensions": [...], "source": [[...], ...]}` — the frontend binds series
  to dimensions with zero reshaping.
- Secrets never round-trip: upstream keys are exposed only as `has_key`; SSH
  key *paths* only, never key material; PTY output is redacted before it is
  surfaced.
- Time buckets are **local time**, and zero-filled across the window so
  category axes stay dense.

### Window and detail params

`window` ∈ `1h | 24h | 7d | 30d | 1y | all`, **or** `from`+`to` (unix
seconds). `all` spans from the earliest recorded request. Unknown values fall
back to `24h`.

`/admin/stats/volume` and `/admin/stats/tokens/timeseries` also take
`detail` ∈ `summary | detailed`, which selects the tick granularity:

| window | `summary` | `detailed` |
| --- | --- | --- |
| `1h` | per minute | per minute |
| `24h` | per hour | per 15 min |
| `7d` | per day | per hour |
| `30d` | per day | per 3 hours |
| `1y` | per month | per day |
| `all` / custom | per day / per hour | same |

All stats endpoints also accept optional `endpoint_id` and `model` filters.

## Objects

### Endpoint
```json
{
  "id": "uuid", "name": "llama.cpp · local", "alias": "local",
  "kind": "local|remote_direct|remote_tunnel",
  "server_type": "llama.cpp|vLLM|ollama|openai|opencode",
  "protocol": "openai|opencode",
  "available_models": [],
  "base_url": "http://127.0.0.1:7070/v1",
  "has_key": false,
  "tunnel_id": null,
  "tunnel_command": "ssh -N -L 9090:localhost:9090 skynet",
  "tunnel_local_port": 9090,
  "priority": 100, "weight": 1, "enabled": true,
  "model": "gemma-4-26B-it", "model_override": null,
  "health": "healthy|degraded|failed|unknown",
  "ewma_latency_ms": 42.1, "last_ok_ts": 1780000000.0,
  "consecutive_fails": 0, "active": true, "share": 0.46,
  "owner": "lcpp", "external_key": "lcpp:7071",
  "meta": {"manager_url": "http://127.0.0.1:7700/"}
}
```

`model` is discovered by the health prober (from `/v1/models`, or
`/config/providers` for `protocol: "opencode"`). `share` is that endpoint's
fraction of requests in the window. `active` marks the router's manual pin.
`kind` is inferred from the URL and tunnel fields when not supplied. Writes
accept `upstream_key` (never returned).

`protocol` is the wire protocol relay speaks to the upstream and the only
field any backend code branches on — `server_type` remains a cosmetic badge
and `kind` is topology, re-derived on every PATCH. Endpoints whose `protocol`
is not `openai` are **alias-only**: never resolved for `model: "auto"`, never
a failover target, never the default pin. See [opencode.md](opencode.md).

`owner`, `external_key` and `meta` are null for endpoints added by hand and
set for ones another program registered (see Registration below).

### Registration

`PUT /admin/registrations/{key}` makes an endpoint exist, idempotently, for a
program that starts and stops servers on its own (lcpp in `~/Models/LLMs`
publishes each llama.cpp server as `lcpp:<port>`). Body:

```json
{
  "name": "minicpm5-2b-7071", "port": 7071, "host": "127.0.0.1",
  "alias": "MiniCPM5-2B", "available_models": ["minicpm-5-2B"],
  "server_type": "llama.cpp", "owner": "lcpp",
  "meta": {"manager_url": "http://127.0.0.1:7700/"}
}
```

Give `port` (URL becomes `http://host:port/v1`) or a full `base_url`.
Response:

```json
{"key": "lcpp:7071", "owner": "lcpp", "adopted": false,
 "skipped_models": [], "endpoint": { /* Endpoint */ }}
```

- Matching: the row holding `key`; else an **unowned** row with the same
  `base_url` is adopted (`adopted: true`), keeping its id (so telemetry
  history), name and alias; else a new row.
- `available_models` entries another endpoint already routes are dropped and
  listed in `skipped_models`. An `alias` collision is a `409`.
- The endpoint is probed before the response returns.
- `DELETE /admin/registrations/{key}` is `204` whether or not the key existed.
- `GET /admin/registrations?owner=lcpp` lists one owner's registrations.

### Model allowlist (`available_models`)

An explicit list of model ids an endpoint serves, protocol-agnostic. Ids match
`^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$`, are deduped case-insensitively,
and must not collide with `auto`, another endpoint's alias, or another
endpoint's allowlist (`422` if they do). Every entry:

- is advertised in `GET /v1/models` as its own model, `owned_by`
  `relay:<endpoint name>`;
- **routes to that endpoint** when a client sends it as `model`, with the id
  forwarded upstream **verbatim** — it outranks `model_override`, since
  naming a model is the point of asking for it;
- narrows the endpoint's discovered catalog, so the default `model` is always
  one the operator permitted. An allowlisted id the prober did not report is
  still offered (a provider catalog can lag what the provider serves).

Send `[]` to clear it. This is what makes one OpenCode server fronting many
provider models individually addressable.

### Model-alias routing

`alias` is a unique routing name (`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`,
`auto` reserved, uniqueness checked case-insensitively). A `/v1` request whose
body `model` matches an alias is **pinned to that endpoint**: relay rewrites
`model` to `model_override` (if set) or the endpoint's discovered default
before forwarding, so the client never needs to know what is actually running
there. Alias-pinned requests do **not** fail over. `model: "auto"` (or any
non-alias value) uses the normal pin / priority-failover resolution.

`GET /v1/models` is synthesized by relay: it lists `auto`, every enabled
endpoint's alias, and every id in an enabled endpoint's `available_models` —
each carrying `relay.endpoint`, `relay.health`, `relay.protocol`, and
`relay.upstream_model` metadata. Everything listed is routable: sending any of
these ids back as `model` reaches the endpoint that advertised it.

### Tunnel (structured, supervised)
```json
{
  "id": "uuid", "name": "gpu-box", "ssh_host": "gpu-box.lan", "ssh_port": 22,
  "ssh_user": "sander", "key_path": "~/.ssh/id_ed25519",
  "remote_host": "127.0.0.1", "remote_port": 8000, "local_port": 8443,
  "compress": true, "keepalive": true, "extra_opts": null, "enabled": false,
  "status": "stopped|starting|up|error", "pid": null,
  "last_error": null, "started_at": null, "uptime_s": 0
}
```

### Tunnel session (interactive, PTY-backed)
```json
{
  "endpoint_id": "uuid", "status": "idle|connecting|awaiting_input|up|error|stopped",
  "prompt": "sander@skynet's password:", "prompt_secret": true,
  "output": ["…redacted recent terminal lines…"],
  "last_error": null, "local_port": 9090, "pid": 41233, "uptime_s": 12.4
}
```

One session per endpoint. Nothing autostarts. When `status` is
`awaiting_input`, POST the answer to `.../respond`; `prompt_secret` means the
UI should mask the field. Sessions for structured tunnels reuse the same
manager under the key `t:<tunnel_id>`, and `endpoint_id` carries that key.

### SSH config host
```json
{
  "alias": "skynet", "hostname": "10.0.0.4", "user": "sander", "port": 22,
  "identity_files": ["~/.ssh/id_ed25519"], "identity_file": "~/.ssh/id_ed25519",
  "identity_explicit": true, "proxyjump": null
}
```
Resolved by running `ssh -G <alias>` — the same resolution ssh itself
performs. `identity_explicit` distinguishes "this host has a configured key"
from "ssh will try every default key".

### Tunnel route
```json
{
  "id": "uuid", "endpoint_id": "uuid", "label": "via bastion",
  "command": "ssh -N -L 9090:localhost:9090 skynet-alt",
  "local_port": 9090, "active": false, "created_ts": 1780000000.0
}
```
A saved candidate ssh command for an endpoint. Activating one copies its
`command`/`local_port` onto the endpoint, so a flaky route can be swapped for
a working one without touching the endpoint's alias or `base_url`.

### Settings
```json
{
  "stream_passthrough": true, "queue_requests": true, "auto_failover": true,
  "log_bodies": false, "allow_cors": true, "inject_stream_usage": true,
  "proxy_port": 4000, "retention_days": 30, "restart_required": false
}
```
`PUT` accepts any subset. `proxy_port` is persisted but only takes effect on
restart, which is what `restart_required` reports.

### Request row (`/admin/stats/recent`)
```json
{
  "id": "req_ab12cd34", "ts": 1780000000.0, "endpoint_id": "uuid",
  "endpoint_name": "llama.cpp · local", "route": "chat.completions",
  "model": "gemma-4-26B-it", "stream": true,
  "status": 200, "ok": true, "state": "streaming|done|error", "error": null,
  "prompt_tokens": 214, "completion_tokens": 512, "total_tokens": 726,
  "ttft_ms": 84.2, "latency_ms": 5230.1, "tokens_per_sec": 99.4,
  "cost_usd": 0.0, "temperature": 0.7, "max_tokens": 1024
}
```
In-flight requests are prepended with `state: "streaming"`, live
`completion_tokens`, and nulls for everything not yet known.

## Stats shapes

| Endpoint | Shape |
| --- | --- |
| `GET /admin/stats/summary` | `{requests, errors, error_rate, prompt_tokens, completion_tokens, total_tokens, cost_usd, ttft_ms:{p50,p95}, latency_ms:{p50,p95}, tokens_per_sec:{p50}}` |
| `GET /admin/stats/volume` | `{dimensions:["time","requests","errors"], source:[["…",312,4],…]}` |
| `GET /admin/stats/tokens/timeseries` | `{dimensions:["time","input","output"], source:[…]}` |
| `GET /admin/stats/tokens/by-hour` | `{dimensions:["hour","input","output"], source:[[0,…,…],…,[23,…,…]]}` (local time, summed across days in window) |
| `GET /admin/stats/tokens/by-day` | `{dimensions:["date","input","output"], source:[["2026-07-01",…,…],…]}` |
| `GET /admin/stats/by-model` | `{dimensions:["model","requests","tokens","cost","avg_tps","errors"], source:[…]}` |
| `GET /admin/stats/by-endpoint` | `{dimensions:["endpoint","requests","input","output","errors","share","endpoint_id"], source:[…]}` |
| `GET /admin/stats/latency` | `{dimensions:["time","ttft_p50","ttft_p95","tps_p50"], source:[…]}` |
| `GET /admin/stats/recent?limit=90` | `{rows:[RequestRow,…], counts:{all,streaming,done,error}}` |
| `GET /admin/stats/live` | `{in_flight, max_concurrency, active, waiting, queue_limit, series:[[ts_ms,n],…]}` |

Percentiles in `summary` are exact (computed from raw rows) for spans up to
~24h and approximate (from stored histograms) for wider spans — see
[architecture.md](architecture.md#storage-and-scale).

## Control-plane results

- `POST /admin/endpoints/{id}/test` → `{ok, latency_ms, models[], error}`.
  The result is also fed into the health state machine as a probe signal.
- `GET /admin/endpoints/health` → `{endpoints:[{id, name, health,
  consecutive_fails, ewma_latency_ms, model}, …], resolved: "<endpoint id>"}`.
- `GET/PUT /admin/router` → `{policy, pinned_id, resolved_id, resolved_name}`.
- `POST /admin/endpoints/{eid}/routes/{rid}/test` → `{ok, latency_ms, error}`
  (quick probe; does not open a real tunnel).
- `POST /admin/tunnels/{id}/test` → `{ssh_ok, ssh_latency_ms, endpoint_ok,
  endpoint_latency_ms, models[], error}`.
- `GET /admin/tunnels/{id}/command` → `{command: "ssh -N -C -L 8443:127.0.0.1:8000 -p 22 -i ~/.ssh/id_ed25519 -o … user@host"}`.
  Cosmetic rendering of the argv list that is actually executed.
- `GET /admin/proxy` → `{base_url, port, example_model, uptime_s,
  requests_total, active_clients, db_size_bytes}`. `base_url` is built from the
  request's `Host` header, so it is copy-pasteable from whatever machine loaded
  the dashboard. `example_model` is a model id the dashboard's snippets can
  actually call: `auto` when an OpenAI-protocol endpoint is registered,
  otherwise the first routable alias (`auto` resolves to nothing on an
  agent-only relay). `db_size_bytes` sums the main DB plus its `-wal` and `-shm`
  files. There is no key field — see Auth.
- `POST /admin/logs/frontend` accepts `{events:[{ts, level, event, detail?}]}`
  (max 200 per batch) → `{accepted: n}`.
- `GET /admin/logs/frontend?limit=100` → recent ingested UI events.

## Auth

**There is none.** `/v1/*` and `/admin/*` both answer any caller that can
reach the port. No API key, no admin token, no accounts, no sessions.

- A caller may still send `Authorization: Bearer …` — most OpenAI SDKs make
  the field mandatory — and relay keeps the last four characters as the
  `client_key` label on the telemetry row. It is not validated against
  anything, and `Authorization` is in the hop-by-hop strip list, so it is
  never forwarded upstream. The target endpoint's own `upstream_key` is
  injected instead.
- The only thing bounding an open relay is the model policy: an OpenCode
  endpoint serves models with `free` in the id and refuses the rest
  (`adapters/opencode.py::is_free_model`). That caps spend. It does **not**
  cap what an agent turn can do on the host it runs on — see
  [deployment.md](deployment.md).
