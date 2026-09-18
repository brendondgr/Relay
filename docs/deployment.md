# Deployment

## Local / single-node (primary target)

Production is one process: FastAPI serves the API **and** the built dashboard.

```bash
cd web/frontend && npm run build          # → web/frontend/dist
cd web/backend && uv sync && \
  uv run uvicorn app.main:app --host 127.0.0.1 --port 4000
```

- Dashboard: `http://127.0.0.1:4000/`
- OpenAI-compatible endpoint: `http://127.0.0.1:4000/v1`

If `web/frontend/dist` is missing the backend still boots — it logs
`frontend dist not found; API-only mode` and serves the API only.

## Development

```bash
./scripts/dev.sh   # backend :4000 (reload) + astro dev :4321 (API proxied)
```

The Astro dev server proxies `/admin`, `/v1`, and `/health` to `:4000`, so the
same paths work in dev and production.

## Configuration (env, prefix `RELAY_`)

Boot-time only — these are read before the DB opens. Runtime-mutable settings
(toggles, retention, proxy port) live in the DB and are edited from the
Settings screen. All of them are optional; the repo-root `.env` is read if it
exists (`app/config.py` points at it explicitly), and so is the environment.

| Var | Default | Purpose |
| --- | --- | --- |
| `RELAY_HOST` | `0.0.0.0` | bind host; reachable from the network by default. `127.0.0.1` keeps it local |
| `RELAY_PORT` | `4000` | proxy/dashboard port (also persisted via Settings) |
| `RELAY_DB_PATH` | `web/backend/data/relay.db` | SQLite database |
| `RELAY_LOG_DIR` | `web/backend/logs` | rotating JSON-lines logs |
| `RELAY_LOG_LEVEL` | `INFO` | log verbosity |
| `RELAY_PROBE_INTERVAL` | `15` | seconds between active health probes |
| `RELAY_PROBE_TIMEOUT` | `5` | probe timeout |
| `RELAY_UNHEALTHY_AFTER` | `3` | consecutive failures → `failed` (out of rotation); request failures alone only reach `degraded` while probes still pass |
| `RELAY_RECOVER_AFTER` | `2` | consecutive probe successes → `healthy` |
| `RELAY_CONNECT_TIMEOUT` | `10` | upstream connect timeout |
| `RELAY_READ_TIMEOUT` | `600` | upstream read timeout (long, for slow streams) |
| `RELAY_WRITE_TIMEOUT` | `60` | upstream write timeout |
| `RELAY_MAX_CONCURRENCY` | `256` | requests at the upstream at once; also the live gauge's ceiling |
| `RELAY_QUEUE_LIMIT` | `2048` | requests allowed to wait for a slot before relay sheds with 429 |
| `RELAY_QUEUE_TIMEOUT` | `30` | seconds a queued request may wait before relay answers 429 itself (`0` waits forever) |
| `RELAY_MAX_BODY_BYTES` | `8388608` | inbound request body cap; larger gets 413 |
| `RELAY_POOL_CONNECTIONS` | derived | upstream HTTP pool size; `0` derives `max_concurrency * 2 + 32` |
| `RELAY_POOL_KEEPALIVE` | derived | idle upstream connections kept; `0` derives `max_concurrency / 2` |
| `RELAY_POOL_TIMEOUT` | `10` | seconds to wait for a pool slot before reporting local saturation |
| `RELAY_DB_THREADS` | `8` | threads serving SQLite (its own pool, not asyncio's default executor) |
| `RELAY_FRONTEND_DIST` | `web/frontend/dist` | built dashboard to serve |

### Sizing under load

`RELAY_MAX_CONCURRENCY` and `RELAY_QUEUE_LIMIT` do different jobs and should
not be tuned together:

- **`max_concurrency` protects the upstream.** It is how many requests reach
  the model server simultaneously. More is not better past the server's own
  capacity — measured against a stub with 0.5 s service time, throughput
  peaked at 256 and then *fell*: 358 rps at 256, 137 rps at 512, 83 rps at
  1024, with p99 latency rising from 4 s to 39 s. Past the knee you are
  paying queueing delay inside the model server instead of inside relay,
  where it is invisible and unbounded. Start at the default and lower it if
  your upstream is a single-process server.
- **`queue_limit` sets how many clients relay can hold.** `max_concurrency +
  queue_limit` is the number of live requests before relay starts refusing;
  the defaults carry ~2300. Requests over the limit get `429` with a
  `Retry-After` rather than a wait nobody is still around for.

Watch `active` and `waiting` on `GET /admin/stats/live`: `waiting` climbing
while `active` sits at `max_concurrency` means the upstream is the
bottleneck, and 429s mean relay has started shedding.

`./scripts/stress.sh` runs this end to end against a throwaway relay and a
stub upstream (`BASELINE=1` runs the same load with relay out of the path, so
the numbers have something to be compared against).

The OpenCode side is configured under its own prefix, and is equally optional:

| Var | Default | Purpose |
| --- | --- | --- |
| `OPENCODE_ENABLED` | `1` | register the agent endpoint at boot |
| `OPENCODE_HOST` / `OPENCODE_PORT` | `127.0.0.1` / `4096` | where `opencode serve` is listening |
| `OPENCODE_ENDPOINT_NAME` | `opencode` | endpoint name relay reconciles |
| `OPENCODE_ALIAS` | `agent` | routing name clients send as `model` |
| `OPENCODE_SERVER_USERNAME` | `opencode` | HTTP Basic user |
| `OPENCODE_SERVER_PASSWORD` | *(generated)* | HTTP Basic password; minted into `OPENCODE_AUTH_FILE` on first launch |
| `OPENCODE_AUTH_FILE` | `web/backend/data/opencode-auth.env` | where that generated credential lives |

Runtime state lives in `web/backend/data/` and `web/backend/logs/` — both
gitignored. Back up the SQLite file to preserve history; note that a live
copy must include the `-wal` and `-shm` sidecar files (or be taken with
`sqlite3 .backup`), since WAL mode keeps recent writes outside the main file.

## Run on startup (systemd user service)

relay runs as a **systemd user service** so the dashboard and proxy come up on
boot. Install with:

```bash
./scripts/install-systemd.sh
```

Pass `--no-start` to install and enable without starting. The units under
`deploy/systemd/` are **templates**: the script substitutes `@REPO_ROOT@` with
this clone's absolute path as it writes them into `~/.config/systemd/user/`, so
the installed units follow wherever the repo actually lives. Edit the templates,
not the installed copies — a re-install overwrites the latter. The script also
enables user lingering (so it starts at boot before login), frees `:4000` if a manual relay
is holding it, and removes the obsolete `vllm-tunnel-skynet.service` if an
earlier install left one behind.

`relay.service` runs `uvicorn app.main:app` on `:4000` with `Restart=always`,
ordered `After` the local llama.cpp router service. It sets `RELAY_DB_PATH` and
`RELAY_LOG_DIR` explicitly so state persists under the repo. relay does **not**
launch the model servers themselves — it just forwards HTTP to their ports.

A `relay` helper wraps the common systemctl calls:

```bash
install -m755 scripts/relay ~/.local/bin/relay
```

```bash
relay status     # service status + /health
relay restart    # e.g. after a code change
relay logs       # journalctl -f
relay enable     # start on boot (enable + linger)
```

Or directly:

```bash
systemctl --user status relay.service
journalctl --user -u relay.service -f
```

To pick up a frontend change, run `npm run build` in `web/frontend`, then
restart the service.

### Tunnels are manual, not on boot

Relay never opens an SSH tunnel by itself — no boot autostart for interactive
sessions, and the structured tunnel supervisor starts with `autostart=False`.
A tunnel-backed endpoint stores a raw `ssh -N -L …` command; you press
**Connect** on the Endpoints screen to open it. The command runs in a
pseudo-terminal, so a host-key confirmation, key passphrase, or password
prompt appears in the page and you answer it there. Nothing typed at a prompt
is stored, and terminal output is redacted before it is surfaced.

### How endpoints survive a reboot

The endpoint registry lives in SQLite, not in code. Registered endpoints
(base URLs, aliases, `model_override`, `tunnel_command`, saved tunnel routes)
are reloaded at startup, so relay comes back knowing where each alias points —
but tunnel-backed endpoints stay disconnected until you connect them.

### Local model servers: lcpp

The llama.cpp servers on this machine are started and stopped by lcpp
(`~/Models/LLMs`, UI and API on `127.0.0.1:7710`), not by relay. lcpp
publishes each one here as it becomes ready, through
`PUT /admin/registrations/lcpp:<port>`, and removes it when it stops. On its
own start it lists `GET /admin/registrations?owner=lcpp` and removes any
registration whose server is gone. So after a reboot, relay still lists
nothing stale even though lcpp loads no models at boot.

Those endpoints carry a **managed · lcpp** badge on the Endpoints screen.
Edits made here last until lcpp next registers them; manage them from lcpp.
A hand-added endpoint whose URL lcpp later registers (the old
`llama.cpp · local` on `:7070`) is adopted, not duplicated. It keeps its id,
alias and request history.

## Security posture

**relay has no authentication.** Not on `/v1`, not on `/admin`. Anyone who can
reach the port can send requests, read every stored prompt and completion, see
each endpoint's URL, and edit the endpoint registry. That is a deliberate
trade for a zero-setup tool, and the default bind is `0.0.0.0`. Know what you
are exposing:

- **An agent endpoint is a shell.** An OpenCode turn runs bash, edits files,
  and writes to disk inside `OPENCODE_PROJECT_DIR`, on the machine hosting it,
  for whoever calls the endpoint. Point that directory at something
  disposable, or set `RELAY_HOST=127.0.0.1`. relay logs a warning at boot when
  an agent endpoint is reachable on a non-loopback bind.
- **Spend is capped, capability is not.** Only OpenCode models with `free` in
  the id are servable (`adapters/opencode.py::is_free_model`), enforced at
  discovery and again at forward time. So an open port cannot run up a model
  bill — but it can still drive the agent.
- For anything beyond a trusted network, put an authenticating proxy
  (Cloudflare Access, a reverse proxy with basic auth, tailscale) in front.
  relay will not do it for you.
- CORS is `*` while `allow_cors` is on.
- Upstream keys are stored server-side and injected on forward. The caller's
  `Authorization` header is stripped as hop-by-hop, so a caller's token can
  never reach an upstream and an upstream key never reaches a client.
- SSH commands are parsed into argv and exec'd directly — never through a
  shell — so a pasted command string is injection-safe. Keys are referenced by
  path; key material is never read or returned.
