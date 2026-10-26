# Cloud providers: Claude, OpenAI and Gemini behind relay's `/v1`

relay fronts the providers' own APIs as endpoints like any other: a client keeps
speaking OpenAI's `/v1/chat/completions`, and relay translates where the
provider speaks something else. Built for GAIFA (plan `llm-servers`, Phase 1);
nothing here needs GAIFA.

## Adding one

```bash
curl -s localhost:4000/admin/endpoints -H 'content-type: application/json' \
  -d '{"name": "Claude", "preset": "anthropic", "upstream_key": "sk-ant-…"}'
```

`preset` is `anthropic`, `openai` or `gemini`: it fills the protocol, the API's
URL, the badge and an alias (`claude`, `openai`, `gemini`), each overridable.
Such an endpoint's kind is `cloud`. The key is stored in `relay.db`
(`upstream_key`) and never returned: endpoints carry `has_key` and `key_hint`
(its last four characters).

A cloud endpoint is **alias-only**: never the `auto` route, the default pin or a
failover target, and never GAIFA's GPU slot. A caller names it.

## Model lists: read live, never hardcoded

`GET /admin/endpoints/{id}/models[?refresh=1]` asks the provider
(`app/services/discovery.py`):

| Protocol | How | What it says |
| --- | --- | --- |
| `anthropic` | the `anthropic` SDK's `models.list()` | context window, output cap, image input, thinking (and which kind), the effort ladder; retired models left out |
| `gemini` | the `google-genai` SDK's `models.list()` | only models that `generateContent`, the `models/` prefix stripped, input/output limits, thinking |
| `openai` | `GET {base}/models` | ids only (vLLM adds `max_model_len`) |

Lists are cached ten minutes and refreshed at start and on demand. A failure
keeps the last good list and says why (`HTTP 401: key rejected — …`); an
unreachable server fails within 2 s. On a cloud OpenAI list, ids naming another
kind of model (embedding, speech, transcription, image, moderation, realtime)
are hidden by a filter you can turn off; any model can be hidden by hand
(`PATCH /admin/endpoints/{id}/models {hidden, hide_filter}`).

## Naming a model: `<alias>/<model>`

`GET /v1/models` lists every visible model of a listed endpoint (cloud
endpoints, and any with `list_models`) as `<alias>/<model>`, e.g.
`claude/claude-opus-5-5`, with the facts the provider reported in `meta` and
`relay.group` (`local`, `remote`, `cloud`, `agent`). Sending that id as `model`
reaches that endpoint with that exact model. Aliases, allowlisted ids and
`auto` work as before; an alias owns its namespace.

## What gets translated

**Claude** (`app/services/adapters/anthropic.py`, the `anthropic` SDK 2.x, API
version 2026-10-21):
- System turns before the first other turn become `system`; later ones stay
  mid-conversation (lifted into `system` on a model that refuses them).
- Images, PDFs, tool calls and tool results become Claude's blocks.
- `max_tokens`, which Claude requires, defaults to the model's own output cap.
- Thinking is adaptive with a readable summary where the model lists it;
  `reasoning_effort` snaps to the model's effort ladder.
- The prompt is cached.
- The server-side refusal fallback (`fallbacks: "default"`) is on.
- `temperature`, `top_p` and `top_k` don't exist on this API version and are
  dropped.

**Gemini** (`app/services/adapters/gemini.py`, the `google-genai` SDK):
- System turns become `system_instruction`.
- Tools keep their whole JSON Schema (`parameters_json_schema`).
- `tool_choice` becomes the function-calling mode.
- Sampling passes through.
- `reasoning_effort` becomes `thinking_level`, or a budget on models that want
  one.
- A blocked prompt or a safety stop is `finish_reason: content_filter` with
  `refusal`, never an empty reply.

**OpenAI** (the passthrough, on a `cloud` endpoint):
- `max_tokens` is sent as `max_completion_tokens`.
- Streams always report usage.
- Relay's own reasoning fields are taken off the messages.

### Hidden reasoning state: `reasoning_details`

Claude's thinking blocks (with their signatures) and Gemini's parts (with their
thought signatures) must come back unchanged on the next request, or a
multi-turn tool conversation fails with a 400. relay returns them as
`reasoning_details` on the assistant message (and on the stream's last delta).
A client that sends the assistant message back as it got it keeps the
conversation valid. Each entry names its `format` (`anthropic`, `gemini`) and
model; another provider ignores them.

### What a model refuses

Some features are accepted by one model and refused by another: forced
`tool_choice`, mid-conversation system messages, the refusal fallback, a
thinking or effort setting, a sampling parameter on an OpenAI reasoning model.
relay sends them. On that model's 400 naming one, it takes it out and makes the
call once more, so there's no table of which model takes what. What it dropped
is in the `X-Relay-Dropped` response header.

### Errors

| What happened | What the client gets |
| --- | --- |
| A 4xx before the reply starts (the request, the key) | The provider's status and message, as `{"error": {...}}`. Only 401/403 count against the endpoint's health. |
| A 5xx, a 529, or the provider couldn't be reached | An error, and the endpoint's health counts it. |
| An error after the stream started | It arrives in the stream, followed by `[DONE]`. |
| A refusal | `finish_reason: content_filter`, with `refusal` set. |

## For chats only (GAIFA)

With `RELAY_CLOUD_REQUIRES_PURPOSE=1` (GAIFA's `service.toml` sets it), a
request routed to a cloud endpoint must send `X-GAIFA-Purpose: chat`, else `403
cloud_not_allowed`. GAIFA's agent sends it on chat turns; its judge, screening
and other background callers don't, so they never reach a paid provider.

## Telemetry

Each request records its `grp` and the prompt tokens read from and written to
the provider's cache (`cached_tokens`, `cache_write_tokens`; Anthropic bills the
writes apart). `GET /admin/stats/recent?group=cloud` shows one kind of server.
Cost uses the settings' price table, as for every endpoint.

## Tests

`tests/backend/fake_providers.py` runs a real HTTP server per test that answers
like the provider, so the official SDKs are exercised for real:
`test_discovery.py`, `test_catalog.py`, `test_adapter_anthropic.py`,
`test_adapter_gemini.py`, `test_openai_cloud.py`, `test_cloud_endpoints.py`.
