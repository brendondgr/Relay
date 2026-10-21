<!-- aihub-setup
id = "relay"
title = "LLM Relay"
models = []
scan = []
summary = "LLM Relay: one OpenAI-compatible endpoint for every chat model, local or remote, with usage charts"
gpu = false
disk_gb = 0.35
needs = ["uv", "npm"]
after = []
check = "aihub check relay"

[[steps]]
run = "aihub install relay"
what = "makes web/backend/.venv/ from uv.lock (about 51 MB) and builds the dashboard with npm (about 290 MB of node_modules/, a few minutes)"
-->

# LLM Relay: setup

The format is in [docs/mods.md §9](../../../docs/mods.md#9-setup-docs).

## What it is

The `relay` mod (port 4000) is the LLM Relay, formerly DashLLM: an OpenAI-compatible proxy with routing, SSH tunnels and usage analytics. It runs no model itself. Every chat model in AIHub goes through it: the agent, the Auto judge and Chat call `http://127.0.0.1:4000/v1`, and the `lcpp` and `vllm` mods publish their running models to it. It has two parts: a FastAPI backend in `web/backend/.venv/`, and an Astro + React dashboard built once into `web/frontend/dist/`, which the backend serves at `/` (the mod's page in AIHub).

## Tested with

Read from this machine on 2026-10-21:

| Part | Version |
|---|---|
| Relay | 0.4.2 |
| Python | 3.12.13 (uv-managed), in `web/backend/.venv/` from `web/backend/uv.lock` |
| fastapi, uvicorn, httpx | 0.139.0, 0.49.0, 0.28.1 |
| pydantic, pydantic-settings | 2.13.4, 2.14.2 |
| Node, npm | 22.23.1, 10.9.8 (host packages; only for the build) |
| astro, @astrojs/react, react, echarts | 5.18.2, 4.4.2, 19.2.7, 5.6.0 (from `web/frontend/package-lock.json`) |

## Prerequisites

- [The hub](../../../docs/setup/hub.md), and `uv` on `PATH`.
- Node 22 and npm, for the dashboard build ([host.md §3](../../../docs/setup/host.md#3-packages)). Without npm the backend still installs and runs; only the page is missing.
- No GPU, no models, no account.
- Optional: [OpenCode](../../opencode/setup/opencode.md), installed first, if you want the `agent` endpoint.

## Install

```bash
aihub install relay
```

Runs `aihub-install.sh`:
1. `uv sync --python 3.12` in `web/backend/` makes `.venv/` from `uv.lock` (about 51 MB).
2. If `web/frontend/dist/` doesn't exist and `npm` is on `PATH`: `npm ci && npm run build` in `web/frontend/` (about 290 MB of `node_modules/` from the npm registry; the built `dist/` is small).

After changing the dashboard's source, rebuild it by hand:

```bash
cd services/relay/web/frontend && npm ci && npm run build
```

### A new machine starts with no upstreams

The relay's endpoints live in its database, `data/databases/relay.db` (gitignored). A fresh one has none. They come from:
- **lcpp**: each llama.cpp instance registers itself as `lcpp:<port>` when it's ready, under its catalog nickname (`gemma4-26B` here), and is removed when it stops. See [llama.cpp](../../lcpp/setup/llama-cpp.md).
- **vllm**: the loaded profile registers under its `alias` (`Qwen3-0.6B-vLLM`, …). See [vLLM](../../vllm/setup/vllm.md).
- **OpenCode**: at start the relay adds an `opencode` endpoint (alias `agent`) when `OPENCODE_ENABLED` is on and it finds the Basic credential that the opencode mod mints. Without the credential it logs a warning and adds nothing.
- **By hand**: the Endpoints view on the relay's page (any OpenAI-compatible server, optionally behind an SSH tunnel).

On this machine, for example, there is also `skynet`: a remote vLLM box reached through an SSH tunnel (`ssh -N -L 127.0.0.1:9090:localhost:9090 skynet`). It exists only here. Tunnels aren't connected at start; they're connected from the Endpoints view.

## Choosing models

The relay runs no model and downloads none, so it has no suggestions. Which models it lists is up to you: any OpenAI-compatible server works, local or remote. For local ones, pick models in [llama.cpp](../../lcpp/setup/llama-cpp.md) (any GGUF) or [vLLM](../../vllm/setup/vllm.md) (Hugging Face repos), and they appear here while they run; for hosted ones, add an endpoint on the page or use [OpenCode](../../opencode/setup/opencode.md)'s free models. Each model is listed under its alias, the name clients send as `model`.

## Configuration

`service.toml` `[run.env]` sets these; it wins over `.env` (architecture §4.4):

| Key | Value | Meaning |
|---|---|---|
| `RELAY_HOST` | `127.0.0.1` | loopback only (the relay has no API key) |
| `RELAY_PORT` | `{port}` (4000) | |
| `RELAY_DB_PATH` | `{databases}/relay.db` | endpoints, settings, request history |
| `RELAY_LOG_DIR` | `web/backend/logs` | the relay's own JSON logs |
| `OPENCODE_AUTH_FILE` | `services/opencode/opencode-auth.env` | the credential the opencode mod mints |

`.env` in this folder (gitignored) is optional: copy `.env.example` only to change something. The backend reads it directly too. The keys that matter under AIHub:

| Key | Required | Meaning |
|---|---|---|
| `OPENCODE_ENABLED` | optional | `1` (default) registers the OpenCode endpoint; `0` if you skip OpenCode |
| `OPENCODE_PORT` | optional | 4096 |
| `OPENCODE_SERVER_USERNAME`, `OPENCODE_SERVER_PASSWORD` | optional | set both only if you set them in the opencode mod's `.env` too; they must match. Unset, the relay reads `OPENCODE_AUTH_FILE` |
| `OPENCODE_ENDPOINT_NAME`, `OPENCODE_ALIAS` | optional | `opencode` and `agent`: the endpoint's name and the model name clients send |
| `RELAY_LOG_LEVEL`, `RELAY_MAX_CONCURRENCY`, `RELAY_QUEUE_LIMIT`, `RELAY_READ_TIMEOUT`, … | optional | the other fields of `Config` in `web/backend/app/config.py`, with the `RELAY_` prefix |

`.env.example` also lists `OPENCODE_PROJECT_DIR` and `OPENCODE_API_KEY`: those are read by the opencode mod, from its own `.env`, not by the relay. The rest (routing, retention, the proxy) is set on the page and kept in the database.

## Check it works

```bash
aihub check relay
```

Expect `relay: all checks passed`. One `warn` line is expected today: `page calls fetch(); use ctx.call / ctx.api …`.

```bash
curl -s http://127.0.0.1:7000/api/s/relay/health
```

Expect `{"status":"ok","version":"0.4.2","uptime_s":…}`.

```bash
curl -s http://127.0.0.1:7000/api/s/relay/v1/models
```

Lists every name you can send as `model`. A fresh install lists only `auto` until an upstream is added. Here (2026-10-21): `auto`, `skynet`, `agent` and OpenCode's free models; `gemma4-26B` appears only while an lcpp instance of it runs.

```bash
curl -s http://127.0.0.1:7000/api/s/relay/admin/endpoints
```

The endpoints with `kind`, `base_url`, `health` and `owner`.

The unit tests (a fake upstream, no network):

```bash
cd services/relay/web/backend && .venv/bin/python -m pytest -q
```

Expect `189 passed` (2026-10-21).

## What to look for

- **No models in Chat.** The relay has no upstream yet: start an lcpp instance, load a vllm profile, or add an endpoint on the page.
- **The `agent` endpoint is missing.** OpenCode wasn't running when the relay started, so no credential was found. Start `opencode`, then `aihub restart relay` (the relay starts after opencode).
- **The page is blank.** `web/frontend/dist/` wasn't built (no npm during install). Run the build above.
- **Don't bind it to `0.0.0.0`.** There is no API key, and the OpenCode endpoint runs shell commands for whoever calls it. AIHub keeps it on `127.0.0.1`.
- `web/backend/data/relay.db` is the standalone DashLLM's default database path. Under AIHub it isn't used (`RELAY_DB_PATH` points to `data/databases/`), but the unit tests may touch it.

## Known gaps

- The endpoints of an existing machine aren't exported or restored by anything but `aihub backup` (it copies `data/databases/`).
- `aihub install relay` doesn't check for Node; without it the dashboard is silently skipped.
