# OpenCode endpoints

Relay can front a running [OpenCode](https://opencode.ai) agent server and
expose it through the ordinary OpenAI API, with full telemetry. Any OpenAI
client works unchanged:

```python
# api_key is required by the SDK, ignored by relay
client = OpenAI(base_url="http://127.0.0.1:4000/v1", api_key="unused")
client.chat.completions.create(
    model="agent",                       # the endpoint's alias
    messages=[{"role": "user", "content": "what changed in this repo today?"}])
```

Only **free** models are served — the ones with `free` in the id. See
[Model policy](#model-policy).

## What relay is actually doing

`opencode serve` is not OpenAI-compatible. It is a stateful agent server, so
relay translates rather than forwards:

| OpenAI (what your client sends) | OpenCode (what relay sends) |
| --- | --- |
| stateless `POST /v1/chat/completions` | `POST /session` → `POST /session/{id}/message` → `DELETE /session/{id}` |
| `"model": "opencode/hy3-free"` | `{"model": {"providerID": "opencode", "modelID": "hy3-free"}}` |
| `messages[].content` | `parts: [{"type": "text", "text": "..."}]` |
| a `role: "system"` message | the message body's top-level `system` field |
| `choices[0].message.content` | concatenated `parts[*].text` |
| `usage.*_tokens` | `info.tokens` (+ `info.cost`, taken as authoritative) |
| *(no credential; relay has no auth)* | `Authorization: Basic base64(user:password)` |

Sessions are **ephemeral**: one per request, deleted in a `finally`, and
`abort`ed first if the client hung up or the turn hit its deadline. Relay
keeps no agent state.

## Setting one up

### The short way: `./launch.sh`

```bash
./launch.sh
```

That is the whole procedure. No `.env`, no arguments. It mints the HTTP Basic
credentials `opencode serve` demands (`scripts/opencode-auth.sh`, stored in
`web/backend/data/opencode-auth.env`), starts both servers, and waits for them
to answer. relay registers the agent endpoint itself during startup and then
discovers the free models the server offers — see
`app/services/opencode_boot.py`. Ctrl-C stops both.

The one setting worth thinking about is `OPENCODE_PROJECT_DIR`: `opencode
serve` binds to one directory and its `bash`/`edit`/`write` tools act there.
It defaults to this repo, which is rarely what you want.

`GET /v1/models` lists what ended up routable; `launch.sh` prints it on
startup too.

If relay is already running as a systemd service on the same port, `launch.sh`
says so; `./launch.sh --takeover` stops the service first.

### Or as services: `relay start | stop | restart`

`./scripts/install-systemd.sh` installs two systemd *user* units —
`relay.service` and `opencode.service` — both reading the same `.env`:

```bash
./scripts/install-systemd.sh
```

`opencode.service` is `PartOf=relay.service` and pulled in by its `Wants=`, so
one command drives both:

| Command | Effect |
| --- | --- |
| `relay start` | starts relay + opencode; relay registers the endpoint at boot |
| `relay stop` | stops both |
| `relay restart` | restarts both |
| `relay status` | unit state for both, relay health, and each endpoint's health |
| `relay logs` | follows both journals |
| `relay enable` / `disable` | on-boot behavior for both |

Add `--relay-only` to any of them to leave the agent server alone.

`OPENCODE_ENABLED=0` in `.env` takes opencode out entirely: `relay` skips the
unit, the unit itself exits without starting if something else launches it,
and relay does not register the endpoint. The wrapper sources the same
`scripts/opencode-auth.sh` as `launch.sh`, so the systemd path and the
foreground path always agree on the password — and neither ever serves an
unauthenticated agent.

Both units take `RELAY_HOST`, `RELAY_PORT`, `OPENCODE_PORT`, and
`OPENCODE_PROJECT_DIR` from `.env` — nothing is baked into the unit files, so a
re-install cannot revert a local change. Re-run
`./scripts/install-systemd.sh` after pulling this change; it also refreshes
`~/.local/bin/relay` if you installed it there.

### Two credentials, neither of them yours to send

relay itself takes no key. The two that exist are both *upstream* of it:

| Credential | Where it comes from | Who checks it |
| --- | --- | --- |
| OpenCode's server password | generated into `web/backend/data/opencode-auth.env`, or pinned with `OPENCODE_SERVER_USERNAME` / `OPENCODE_SERVER_PASSWORD` | `opencode serve`, as HTTP Basic. relay presents it as the endpoint's `upstream_key`. |
| The provider's API key | `OPENCODE_API_KEY` (or `opencode auth login`) | OpenCode Zen, when the agent calls the model. `launch.sh` exports it into the opencode process. |

The provider prefix on a model id says which provider credential is in play —
`opencode/…` wants `OPENCODE_API_KEY`. A locally hosted provider needs none.

### The manual way

1. Start the server in the project you want the agent to work in:

   ```bash
   OPENCODE_SERVER_USERNAME=me OPENCODE_SERVER_PASSWORD=s3cret \
     opencode serve --port 4096
   ```

2. In the dashboard, **Endpoints → + Add endpoint**:

   | Field | Value |
   | --- | --- |
   | type | `OpenCode (agent)` |
   | alias | e.g. `agent` — the routing name clients send as `model` |
   | url | `http://127.0.0.1:4096` (**no** `/v1` suffix) |
   | key | `user:password`, or a bare password for the default user `opencode` |
   | available models | one `provider/model` id per line — free ids only |

   The key must match the server's `OPENCODE_SERVER_USERNAME` /
   `OPENCODE_SERVER_PASSWORD`.

3. **Test connection** hits `/global/health` and reads `/config/providers`.

## Model policy

**Free models only.** A model is servable if its id contains `free`
(`adapters/opencode.py::is_free_model`) — `opencode/hy3-free`,
`opencode/nemotron-3-ultra-free`, and so on. Anything else is refused with a
`400`.

That rule is what makes an unauthenticated relay tolerable: relay has no auth
layer, so whoever reaches the port spends whatever the OpenCode account is
authenticated for. Capping the model set caps the bill. It is enforced twice —
paid models are dropped from the catalog at discovery, so they never reach the
router or `/v1/models`, and `forward()` refuses one again even if a paid id
was typed into an endpoint's allowlist or set as its `model_override`.

Discovery publishes what it finds as the endpoint's **available models** list,
which is what makes each one individually addressable:

- `GET /v1/models` lists every free model under the alias, as
  `agent/opencode/hy3-free`, read live from `GET /config/providers` (cached
  ten minutes, `GET /admin/endpoints/{id}/models?refresh=1` asks again) with
  the window, tools, images and thinking OpenCode reports (`relay.group` is
  `agent`). A server that stops answering keeps its last list, with the error.
- `"model": "agent/opencode/hy3-free"` routes to this endpoint and forwards
  exactly `opencode/hy3-free`.
- `"model": "opencode/hy3-free"`, the older form, still routes and forwards
  **exactly** that model (the list outranks `model_override`); it is no
  longer listed a second time.
- `"model": "agent"` uses the endpoint's default: `model_override` if it is
  free, otherwise the first discovered free model. The alias never falls
  through to OpenCode's *own* default, which is normally a paid one.
- The ids cannot collide with another endpoint's alias or allowlist.

The list is reconciled from the server at every boot. Narrowing it by hand in
the dashboard holds until the next restart.

Clients may also pass a non-standard `"agent"` field (`build`, `plan`, a
custom agent) in the request body; it is forwarded when present.

## Things that will bite you

- **Agent turns are not cheap in prompt tokens.** OpenCode sends its system
  prompt and tool definitions with every turn, so a one-word question bills
  5,000–10,000 prompt tokens before your text is counted. That is inherent to
  driving an agent, not relay overhead, but it shows up in the dashboard and
  it is worth knowing before you point a chat UI at it.

- **A provider error arrives as a `200` with `finish_reason: "error"`.** The
  agent turn itself succeeded; the model call inside it failed. The detail is
  in `relay.agent_error` on the response body (e.g. "No payment method"), and
  the row lands in telemetry with zero tokens.

- **Set `permission` explicitly.** A permission left at `"ask"` parks the turn
  waiting for a human answer that relay cannot give. Relay bounds it with a
  15-minute deadline (`_TURN_TIMEOUT`), then aborts the session and returns
  `504` — but the request has burned a concurrency slot until then. Configure
  the permissions you actually want instead of relying on the deadline.

- **This is remote code execution by design.** OpenCode's `bash`, `edit`, and
  `write` tools run against the directory the server was started in. relay has
  no auth, so anyone who can reach the port can drive them — the free-model
  policy caps the bill, not the capability. Point `OPENCODE_PROJECT_DIR` at
  something disposable, or set `RELAY_HOST=127.0.0.1`. relay logs a warning at
  boot when an agent endpoint is reachable on a non-loopback bind.

- **One server = one project.** `opencode serve` is bound to its launch
  directory. Several projects means several servers means several relay
  endpoints, each with its own alias.

- **Long turns hold a concurrency slot.** `max_concurrency` (18) is shared
  with every other endpoint; agent turns can occupy a slot for minutes.

- **Agent endpoints are alias-only.** They are never chosen for
  `model: "auto"`, never a failover target, and never the default pin. Marking
  one "active" in the dashboard does not redirect `auto` traffic to it.

- **`/v1/embeddings` and `/v1/completions` return `501`.** An agent server
  implements neither.

## Streaming

`stream: true` works and emits valid OpenAI SSE, but it is **synthesized**:
relay awaits the blocking turn, then re-emits it as a role chunk, a content
chunk, a finish chunk, a `usage` chunk, and `[DONE]`. TTFT therefore equals
total latency, which is honest — the upstream produced nothing earlier.

Real deltas would mean subscribing to OpenCode's `GET /event` stream, which is
*global*, not per-session: one shared subscription demultiplexed across
concurrent requests. That is real state in an otherwise stateless proxy, and
it stays out until it earns its keep.

## Telemetry

Everything the dashboard shows works for agent turns. Token counts come from
`info.tokens` (reasoning folded into completion tokens), and **cost comes from
`info.cost`** rather than relay's price table — OpenCode already priced the
turn against the real provider's rates. Tool invocations appear inline in the
completion text as `[tool: bash]` markers; they are not mapped to OpenAI
`tool_calls`, because a guessed mapping would look valid to a client and be
wrong.
