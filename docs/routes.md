# Route Map

Generated from `web/backend/app/routes/` and `app/main.py`. Payload shapes
live in [api-contract.md](api-contract.md).

## Frontend pages (Astro)

| Route | File | Purpose |
| --- | --- | --- |
| `/` | `web/frontend/src/pages/index.astro` | Single-page dashboard; all six screens are client-side state within one React island — no URL routing |

Screens inside the island: Dashboard, Requests, Endpoints, SSH Tunnel,
Proxy Info, Settings.

## Backend — OpenAI-compatible client plane

`app/routes/v1.py` registers a **single catch-all**
(`ANY /v1/{path:path}`, methods GET/POST/PUT/DELETE/PATCH/HEAD) that hands off
to the proxy service. There is no authentication on it. The proxy
classifies the path itself, so these are behaviors rather than separate route
declarations:

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/v1/chat/completions` | primary; streaming + non-streaming, fully instrumented (`route: "chat.completions"`) |
| POST | `/v1/completions` | instrumented; `501` if the resolved endpoint's protocol doesn't implement it |
| POST | `/v1/embeddings` | instrumented; `501` if the resolved endpoint's protocol doesn't implement it |
| GET | `/v1/models` | **synthesized by relay**, never forwarded: `auto` + every enabled endpoint alias + every endpoint's `available_models` |
| ANY | `/v1/{path}` | generic passthrough, recorded coarsely under its path name |

## Backend — control plane

No `/admin/*` route is authenticated — there is no admin plane. See
[api-contract.md](api-contract.md#auth).

### Endpoints — `app/routes/admin_endpoints.py`

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/admin/endpoints` | list + live health + 24h share |
| POST | `/admin/endpoints` | create → 201 |
| PATCH | `/admin/endpoints/{eid}` | edit any field (url, alias, key, priority, enabled, model_override, tunnel_command…) |
| DELETE | `/admin/endpoints/{eid}` | remove → 204 |
| POST | `/admin/endpoints/{eid}/activate` | hot-swap: pin as active → `RouterState` |
| POST | `/admin/endpoints/{eid}/test` | probe now → `{ok, latency_ms, models[], error}`; also feeds the health machine |
| GET | `/admin/endpoints/health` | pool snapshot + currently resolved id |
| GET/PUT | `/admin/router` | policy (`manual`\|`priority`) + pin + resolved target |

### Interactive tunnel sessions (per endpoint)

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/admin/endpoints/tunnel-sessions` | status of every live session |
| GET | `/admin/endpoints/{eid}/tunnel` | one endpoint's session status |
| POST | `/admin/endpoints/{eid}/tunnel/connect` | run its `tunnel_command` in a PTY; 422 if none is set |
| POST | `/admin/endpoints/{eid}/tunnel/disconnect` | terminate the session |
| POST | `/admin/endpoints/{eid}/tunnel/respond` | answer a prompt; 409 if nothing is awaiting input |

### Saved tunnel routes (candidate ssh commands per endpoint)

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/admin/endpoints/{eid}/routes` | list |
| POST | `/admin/endpoints/{eid}/routes` | create → 201 |
| PATCH | `/admin/endpoints/{eid}/routes/{rid}` | edit label/command |
| DELETE | `/admin/endpoints/{eid}/routes/{rid}` | remove → 204 |
| POST | `/admin/endpoints/{eid}/routes/{rid}/activate` | copy onto the endpoint → `EndpointOut` |
| POST | `/admin/endpoints/{eid}/routes/{rid}/test` | quick probe without opening a tunnel |

### Registrations — `app/routes/admin_registrations.py`

Idempotent, owner-scoped endpoints for programs that start servers on their
own (lcpp). See `services/registrations.py` for matching and adoption.

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/admin/registrations?owner=…` | registrations, optionally one owner's |
| PUT | `/admin/registrations/{key}` | upsert by key → `RegistrationOut`; adopts an unowned endpoint with the same URL; 409 on alias clash |
| DELETE | `/admin/registrations/{key}` | remove → 204, also when already gone |

### SSH config

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/admin/ssh/hosts` | host aliases from `~/.ssh/config` |
| GET | `/admin/ssh/resolve?host=…` | resolve one alias via `ssh -G` |

### Structured tunnels — `app/routes/admin_tunnels.py`

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/admin/tunnels` | list with live status |
| POST | `/admin/tunnels` | create → 201 |
| PATCH | `/admin/tunnels/{tid}` | edit (incl. `ssh_port` / `local_port`) |
| DELETE | `/admin/tunnels/{tid}` | remove → 204 |
| POST | `/admin/tunnels/{tid}/start` · `/stop` | supervised lifecycle |
| POST | `/admin/tunnels/{tid}/test` | `{ssh_ok, endpoint_ok, models[], error}` |
| GET | `/admin/tunnels/{tid}/command` | exact `ssh -N -L …` display string |
| GET | `/admin/tunnels/{tid}/session` | PTY session status |
| POST | `/admin/tunnels/{tid}/session/connect` · `/disconnect` · `/respond` | interactive PTY control (keyed `t:<tid>`) |

### Settings, proxy info, logs — `app/routes/admin_settings.py`

| Method | Path | Notes |
| --- | --- | --- |
| GET/PUT | `/admin/settings` | toggles, retention, proxy port |
| GET | `/admin/proxy` | base url (built from the request's Host header), uptime, totals, DB size |
| POST | `/admin/logs/frontend` | frontend UI event ingestion (≤200 per batch) |
| GET | `/admin/logs/frontend?limit=100` | read back recent UI events |

### Liveness — `app/main.py`

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/health` | `{status, version, uptime_s}` |

## Backend — stats plane (ECharts-shaped)

`app/routes/admin_stats.py`, prefix `/admin/stats`.

| Path | Feeds |
| --- | --- |
| `/summary` | KPI row |
| `/volume` | Request volume series (accepts `detail`) |
| `/tokens/timeseries` | Tokens in/out series (accepts `detail`) |
| `/tokens/by-hour` | Tokens by hour-of-day bars |
| `/tokens/by-day` | Tokens per day bars |
| `/by-model` | Model breakdown |
| `/by-endpoint` | Endpoint breakdown bars |
| `/latency` | TTFT / tok-per-sec trend |
| `/recent?limit=90` | Requests screen table (in-flight rows first) |
| `/live` | In-flight gauge + concurrency series |

Common query params: `window` ∈ {`1h`,`24h`,`7d`,`30d`,`1y`,`all`} or
`from`/`to` (unix seconds); optional `endpoint_id`, `model`. `/recent` takes
only `limit`; `/live` takes none.

## Static dashboard

When `web/frontend/dist` exists, `app/main.py` mounts it at `/` with
`StaticFiles(html=True)`. The mount is added **after** all routers, so API
paths always win.
