<p align="center">
  <img src="assets/header.svg" width="880"
       alt="Relay: LLM Dashboard. The mark is a letter R whose stem is a cyan arrow pointing up beside an amber arrow pointing down. One OpenAI-compatible /v1 in front of local model servers and agent servers, and a record of what every request through it cost in latency and tokens. Written in Python, TypeScript and Shell, on FastAPI, Astro, React, ECharts, SQLite and uv." />
</p>

<!-- The banner repeats the sentence below. Change one, change both, then run
     python3 tools/make_header.py -->

An OpenAI-compatible **LLM proxy** with a telemetry **dashboard**. relay sits
between your clients and one or more model servers (llama.cpp, vLLM, ollama,
any OpenAI-compatible endpoint), exposes its own drop-in `/v1` endpoint, and
records per-request telemetry rendered as Apache ECharts visualizations.

![Dashboard: stat tiles for requests, tokens in and out, tokens per second, p95 latency and error rate, above per-hour request volume, live concurrency against the cap, and a per-endpoint breakdown.](docs/assets/Dashboard.png)

*318.5k requests over a two-month range. Volume and tokens per hour on the left,
live concurrency against the 256 cap on the right, per-endpoint totals below it.*

**Status:** active. Self-hosted and in daily use since July 2026 — this is the
proxy my own research traffic actually goes through, not a demonstration of one.

## Why this exists

Three problems, in the order they turned up. Running models locally, there was
no way to see what any of it cost: no time-to-first-token, no tokens per second,
no record of which prompts were slow or which ones failed. Then there were
several servers, on different machines and different ports, and every client
needed rewiring whenever one moved or died. Then OpenCode, which is an agent
server that does not speak the OpenAI protocol at all.

relay is the one place all three are handled. Clients get a stable `/v1` that
outlives whatever is behind it, routing is a live registry rather than a config
file, and every request that passes through leaves a row behind. The dashboard
is the part I actually use — it is how I find out that a change made generation
slower, which I could not see before.

## What it does

- **Proxy** — streaming SSE passthrough with TTFT and token-usage capture,
  multi-endpoint registry, hot-swap, health detection, priority failover, and
  model-alias routing that pins a request to a named server.
- **Remote hosting** — interactive SSH tunnels run in a real pseudo-terminal,
  so host-key confirmations, passphrases, and password prompts are answered
  in the dashboard. Nothing connects on its own.
- **Agent upstreams** — an [OpenCode](https://opencode.ai) server is
  registered automatically at boot; relay translates OpenAI chat requests into
  agent sessions, and every free model the server offers is individually
  addressable. See [docs/opencode.md](docs/opencode.md).
- **Registrations** — a program that starts model servers on its own can
  publish and withdraw them idempotently by key (`PUT/DELETE
  /admin/registrations/{key}`). [lcpp](https://github.com/brendondgr/LLMs),
  the llama.cpp launcher on the same machine, registers each server it starts
  as `lcpp:<port>`. Those endpoints are marked *managed* on the Endpoints
  screen. See [deployment.md](docs/deployment.md).
- **Dashboard** — Astro + React + ECharts, fed by an ECharts-shaped stats API
  (`dimensions`/`source` payloads) backed by hourly rollups.

![Requests screen: a live table of calls with time, status, model and endpoint, tokens in and out, tokens per second, and end-to-end duration.](docs/assets/ModelCalls.png)

*Every call as it lands. Each row expands to the prompt and completion relay
stored for it, which is the part that makes a slow generation diagnosable after
the fact rather than during it.*

![Endpoints screen: four registered vLLM endpoints with health dots, URLs, model names and average latency, one marked active.](docs/assets/ServerEndpoints.png)

*Four model servers registered. Requests route to the active one; hot-swapping
drains in-flight requests before switching, so nothing in progress is dropped.*

## Quickstart

```bash
./launch.sh
```

Nothing to configure. That starts `opencode serve` and relay together,
generates the credentials the agent server needs, registers the agent endpoint,
and discovers the free models it offers. Point any OpenAI client at
`http://127.0.0.1:4000/v1` — **there is no API key**:

```bash
curl http://127.0.0.1:4000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "agent", "messages": [{"role": "user", "content": "hi"}]}'
```

`GET /v1/models` lists everything routable: the `agent` alias, every free
OpenCode model by name, and any other endpoint you register. Build the
dashboard once (`cd web/frontend && npm install && npm run build`) and the same
process serves it at `http://127.0.0.1:4000/`.

relay is unauthenticated and binds `0.0.0.0` by default, and an OpenCode turn
runs shell commands on its host. Read the security posture in
[deployment.md](docs/deployment.md) before putting it on a network you don't
control.

Backend alone, no agent server:

```bash
cd web/backend && uv sync && uv run uvicorn app.main:app --port 4000
```

For frontend work, run both dev servers instead — Astro on `:4321` proxies
`/admin`, `/v1`, and `/health` to the backend on `:4000`:

```bash
./scripts/dev.sh
```

## Layout

```text
root/
├── launch.sh                # relay + opencode together, zero config
├── .env.example             # optional overrides (.env is gitignored)
├── docs/                    # architecture, api-contract, routes, data-flow,
│                            # frontend, deployment, opencode
├── web/
│   ├── backend/             # FastAPI service (uv project)
│   │   ├── app/
│   │   │   ├── main.py      # app factory, lifespan, static dashboard serving
│   │   │   ├── config.py    # pydantic-settings (RELAY_* / OPENCODE_* env)
│   │   │   ├── db.py        # SQLite schema, migrations, rollup backfill
│   │   │   ├── security.py  # bearer extraction + masking (telemetry labels)
│   │   │   ├── core/        # logging.py
│   │   │   ├── routes/      # v1.py + admin_{endpoints,settings,stats,tunnels}.py
│   │   │   ├── services/    # proxy, router, health, stats, rollup, histogram,
│   │   │   │                # telemetry, settings_store, opencode_boot,
│   │   │   │                # tunnels, tunnel_sessions, ssh_config
│   │   │   └── schemas/     # Pydantic models (the API contract)
│   │   ├── data/            # runtime SQLite (gitignored)
│   │   ├── logs/            # rotating JSON-lines logs (gitignored)
│   │   └── pyproject.toml
│   └── frontend/            # Astro + React + ECharts dashboard
│       ├── src/
│       │   ├── pages/       # index.astro (single page)
│       │   ├── layouts/     # Base.astro (fonts, global CSS, keyframes)
│       │   ├── components/  # App shell, EChart host, screens/
│       │   ├── lib/         # api, types, chartOptions, styles, format,
│       │   │                # logger, echarts
│       │   └── hooks/       # usePoll
│       ├── astro.config.mjs
│       └── package.json
├── tests/backend/           # pytest suite (170 tests, fake upstreams)
├── utils/                   # seed_telemetry.py, stub_upstream.py, stresstest.py
├── scripts/                 # dev/launch wrappers, systemd install, stress + live checks
└── deploy/systemd/          # relay.service + opencode.service (user units)
```

## Tests

```bash
cd web/backend && uv run pytest
```

`pyproject.toml` points `testpaths` at `../../tests/backend`, so pytest must
be run from `web/backend`. The suite uses a fake upstream ASGI app — no real
model server or SSH host is needed.

Against live model servers:

```bash
./scripts/validate_live.sh
```

## Docs

| Doc | Covers |
| --- | --- |
| [architecture.md](docs/architecture.md) | Subsystems, routing resolution, health state machine, storage/rollup strategy |
| [api-contract.md](docs/api-contract.md) | Object shapes, stats payloads, why there is no auth |
| [routes.md](docs/routes.md) | Every HTTP route |
| [data-flow.md](docs/data-flow.md) | Hot path, poll loops, control-plane writes, logging |
| [opencode.md](docs/opencode.md) | Fronting an OpenCode agent server: setup, the free-model policy, gotchas |
| [frontend.md](docs/frontend.md) | Component map, design tokens, chart conventions |
| [deployment.md](docs/deployment.md) | Single-process production, env vars, systemd, security posture |
| [plans/](docs/plans/) | Design records from before the OpenCode work shipped. Historical — kept as written, not updated |

`CLAUDE.md` holds working conventions for agents (and humans) editing this
repo.
