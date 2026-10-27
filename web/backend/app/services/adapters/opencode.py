"""OpenCode adapter: OpenAI chat-completions in, agent sessions out.

``opencode serve`` is not OpenAI-compatible. It is a stateful agent server:
work is a *session* you create, send parts to, and delete; the reply is
``{info, parts}``, not ``{choices}``; usage lives on ``info.tokens`` and comes
pre-priced in ``info.cost``; auth is HTTP Basic, not Bearer.

Design choices worth knowing before you edit this:

- **Ephemeral sessions.** Create → message → delete, once per request, with an
  ``abort`` first if the client hung up. Relay stays stateless and OpenAI
  semantics stay exact; the cost is one extra round trip either side of the
  turn. A session cache would be faster and would leak agent state between
  unrelated callers.
- **Flattened history.** OpenCode has no way to post an assistant turn *as* an
  assistant turn, so a multi-turn ``messages[]`` is rendered into one text part
  as a labeled transcript. Lossy, but a single call and predictable. The
  faithful alternative (replaying each prior turn with ``noReply``) costs N+1
  round trips and still can't type the assistant turns.
- **Synthesized streaming.** The message POST blocks; real deltas only exist on
  a *global* ``GET /event`` stream that would need one shared subscription
  demultiplexed across concurrent requests. Until that earns its keep, a
  ``stream: true`` request gets the finished turn re-emitted as valid OpenAI
  SSE. TTFT therefore equals total latency — honest for a blocking upstream.
- **Tool calls are summarized as text** (``[tool: bash]``), not mapped to
  OpenAI ``tool_calls``. A guessed mapping would look valid to a client and be
  wrong.

Operational note: an OpenCode instance behind relay must have its
``permission`` config set explicitly. A permission set to ``"ask"`` parks the
turn forever — there is no human on this side to answer it — which is what
``_TURN_TIMEOUT`` exists to bound.

**Only free models are served.** relay has no auth layer, so anyone who can
reach the port can spend whatever the OpenCode account is authenticated for.
The one thing standing between an open port and a real bill is this filter:
a model is usable only if its id contains ``free`` (see ``is_free_model``).
It is enforced twice on purpose — the probe never publishes a paid model, and
``forward`` refuses one even if a paid id was written into an endpoint's
allowlist by hand.
"""

import asyncio
import base64
import json
import time
import uuid

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.core.logging import get_logger
from app.schemas import EndpointTestResult
from app.services.adapters.base import (
    RetryableUpstreamError,
    UpstreamAdapter,
    classify_httpx_error,
)
from app.services.telemetry import RequestRecord

log = get_logger("opencode")

# Hard ceiling on one agent turn. Long by HTTP standards and deliberately so —
# an agent turn legitimately runs minutes — but finite, because a misconfigured
# `permission: "ask"` otherwise hangs the request (and its concurrency slot)
# forever.
_TURN_TIMEOUT = 900.0
_SETUP_TIMEOUT = 20.0  # session create/delete/abort: fast or broken
_BODY_LIMIT = 100_000

_DEFAULT_BASIC_USER = "opencode"


def _basic_auth(upstream_key: str | None) -> dict[str, str]:
    """OpenCode authenticates with HTTP Basic. ``upstream_key`` is stored as
    ``user:password``; a bare value is the password for the default user."""
    if not upstream_key:
        return {}
    user, sep, password = upstream_key.partition(":")
    if not sep:
        user, password = _DEFAULT_BASIC_USER, upstream_key
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def split_model(model: str | None) -> tuple[str, str] | None:
    """``"anthropic/claude-sonnet-4-5"`` -> ``("anthropic", "claude-sonnet-4-5")``.

    Returns None when there is no provider prefix, in which case the model is
    omitted from the message body and OpenCode picks its configured default —
    the same "let the server decide" behavior an endpoint with no
    ``model_override`` gets on the OpenAI path.
    """
    if not model:
        return None
    provider, sep, model_id = model.partition("/")
    if not sep or not provider or not model_id:
        return None
    return provider, model_id


def flatten_messages(messages: list[dict]) -> tuple[str, str | None]:
    """OpenAI ``messages[]`` -> (prompt text, system text).

    System messages are joined into OpenCode's top-level ``system`` field.
    Everything else becomes one text part: the trailing user message verbatim,
    prefixed by any earlier turns as a labeled transcript.
    """
    system: list[str] = []
    turns: list[tuple[str, str]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or "user"
        content = msg.get("content")
        if isinstance(content, list):
            # multimodal content blocks: keep the text, drop the rest
            content = "".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text")
        if not isinstance(content, str) or not content:
            continue
        if role == "system":
            system.append(content)
        else:
            turns.append((role, content))

    if not turns:
        prompt = ""
    elif len(turns) == 1:
        prompt = turns[0][1]
    else:
        *history, (_, last) = turns
        transcript = "\n\n".join(
            f"{'User' if role == 'user' else 'Assistant'}: {text}"
            for role, text in history)
        prompt = f"{transcript}\n\nUser: {last}"
    return prompt, ("\n\n".join(system) or None)


def is_free_model(model: str | None) -> bool:
    """Free models are the ones whose id says so — ``opencode/hy3-free``,
    ``opencode/nemotron-3-ultra-free``. That naming convention is the whole
    rule; the provider half of the id is ignored so a provider called "free"
    can't wave a paid model through.

    Change this function and you change what an open relay can be billed for.
    """
    if not model:
        return False
    return "free" in model.rpartition("/")[2].lower()


def catalog_models(payload: dict, free_only: bool = True) -> list[str]:
    """``GET /config/providers`` -> ``["opencode/hy3-free", ...]``."""
    return [mid for mid, _ in catalog_entries(payload, free_only)]


def catalog_entries(payload: dict, free_only: bool = True) -> list[tuple[str, dict]]:
    """``GET /config/providers`` -> ``[("opencode/hy3-free", {its entry}), ...]``.

    ``providers[].models`` is a map keyed by model id in the current server;
    a list is accepted too so a shape change doesn't blank the catalog.
    Paid models are dropped here rather than at the door, so they never reach
    the router, ``/v1/models``, or the dashboard in the first place. The entry
    carries what OpenCode knows of the model (``limit``, ``tool_call``, …).
    """
    out: list[tuple[str, dict]] = []
    for provider in payload.get("providers") or []:
        if not isinstance(provider, dict):
            continue
        pid = provider.get("id") or provider.get("providerID")
        if not pid:
            continue
        models = provider.get("models")
        if isinstance(models, dict):
            items = [(k, v if isinstance(v, dict) else {}) for k, v in models.items()]
        elif isinstance(models, list):
            items = [(m.get("id"), m) for m in models
                     if isinstance(m, dict) and m.get("id")]
        else:
            items = []
        out.extend((f"{pid}/{mid}", info) for mid, info in items if mid)

    if free_only:
        out = [e for e in out if is_free_model(e[0])]

    # Surface the server's configured default first: relay treats models[0] as
    # the endpoint's default model. A paid default (OpenCode's usually is) has
    # already been filtered out, in which case the first free model wins.
    default = payload.get("default")
    if isinstance(default, dict):
        for pid, mid in default.items():
            wanted = f"{pid}/{mid}"
            hit = next((e for e in out if e[0] == wanted), None)
            if hit is not None:
                out.remove(hit)
                out.insert(0, hit)
            break
    return out


# `info.finish` carries the Vercel AI SDK's finishReason vocabulary, which is
# close to OpenAI's but not identical. Anything unrecognized is reported as
# "stop": a made-up finish_reason is worse than a slightly lossy true one.
_FINISH_REASONS = {
    "stop": "stop",
    "length": "length",
    "tool-calls": "tool_calls",
    "content-filter": "content_filter",
    "error": "error",
}


def _finish_reason(finish, error) -> str:
    mapped = _FINISH_REASONS.get(str(finish).lower()) if finish else None
    if mapped:
        return mapped
    if error:
        return "length" if "output" in str(error).lower() else "error"
    return "stop"


def extract_text(parts: list) -> tuple[str, list[str]]:
    """Assistant parts -> (visible text, tool names used).

    Tool invocations are rendered inline as ``[tool: name]`` markers rather
    than dropped, so a caller can see the agent did something between words.
    """
    chunks: list[str] = []
    tools: list[str] = []
    for part in parts or []:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text" and part.get("text"):
            chunks.append(part["text"])
        elif kind == "reasoning" and part.get("text"):
            continue  # reasoning is metered but not returned as content
        elif kind == "tool":
            name = part.get("tool") or "tool"
            tools.append(name)
            chunks.append(f"\n[tool: {name}]\n")
    return "".join(chunks), tools


class OpenCodeAdapter(UpstreamAdapter):
    name = "opencode"

    # An agent server answers chat turns. It has no embeddings model and no
    # legacy text-completion route; `models` is synthesized by the proxy and
    # never reaches an adapter.
    _ROUTES = frozenset({"chat.completions"})

    def supports(self, route: str) -> bool:
        return route in self._ROUTES

    # ---- health ---------------------------------------------------------
    async def probe(self, http: httpx.AsyncClient, endpoint: dict,
                    timeout: float) -> EndpointTestResult:
        base = endpoint["base_url"]
        headers = _basic_auth(endpoint.get("upstream_key"))
        t0 = time.perf_counter()
        try:
            health = await http.get(f"{base}/global/health", headers=headers,
                                    timeout=timeout)
            latency = (time.perf_counter() - t0) * 1000
            if health.status_code == 401 or health.status_code == 403:
                return EndpointTestResult(
                    ok=False, latency_ms=round(latency, 1),
                    error=f"HTTP {health.status_code}: check the Basic auth "
                          "credential (user:password)")
            if health.status_code >= 400:
                return EndpointTestResult(
                    ok=False, latency_ms=round(latency, 1),
                    error=f"HTTP {health.status_code} from /global/health")

            models: list[str] = []
            try:
                providers = await http.get(
                    f"{base}/config/providers", headers=headers,
                    timeout=timeout)
                if providers.status_code == 200:
                    models = catalog_models(providers.json())
            except (httpx.HTTPError, ValueError) as e:
                # Liveness already passed; an unreadable catalog is not a
                # failed endpoint, just an endpoint with no discovered models.
                log.debug("provider catalog unavailable", extra={"data": {
                    "endpoint": endpoint["name"], "error": str(e)[:200]}})

            log.debug("probe ok", extra={"data": {
                "endpoint": endpoint["name"], "latency_ms": round(latency, 1),
                "models": len(models)}})
            return EndpointTestResult(
                ok=True, latency_ms=round(latency, 1), models=models)
        except httpx.HTTPError as e:
            latency = (time.perf_counter() - t0) * 1000
            return EndpointTestResult(
                ok=False, latency_ms=round(latency, 1),
                error=f"{type(e).__name__}: {e}")

    # ---- model policy ----------------------------------------------------
    def _pick_model(self, proxy, endpoint: dict,
                    requested: str | None) -> tuple[str | None, str | None]:
        """Resolve the turn's model, or refuse. Returns (model, refusal).

        Two cases, and the second is the one that matters:

        - The caller named a concrete ``provider/model``. Serve it if it is
          free, refuse it otherwise — including when it arrived via an
          endpoint's ``model_override``, which the dashboard can set by hand.
        - The caller used the alias (no provider prefix). Substituting the
          endpoint's first free model is not a nicety: omitting ``model`` from
          the message lets *OpenCode* choose, and its configured default is
          normally a paid one.
        """
        st = proxy.router.state.get(endpoint["id"])
        free = [m for m in (st.models if st else []) if is_free_model(m)]

        if requested and split_model(requested):
            if is_free_model(requested):
                return requested, None
            return None, (
                f"model {requested!r} is not available: this relay serves only"
                " free models"
                + (f" ({', '.join(free)})" if free else "")
                + ". See GET /v1/models.")

        if free:
            return free[0], None
        return None, (
            "no free model is available from this OpenCode server — none of"
            " the models it offers have 'free' in the id, or the provider"
            " catalog has not been read yet")

    def _reject(self, proxy, record: RequestRecord, t0: float, message: str,
                short: str | None = None) -> JSONResponse:
        record.status = 400
        record.ok = False
        record.error = short or message
        record.latency_ms = (time.perf_counter() - t0) * 1000
        proxy.telemetry.submit(record)
        return JSONResponse(status_code=400, content={"error": {
            "message": message, "type": "relay_proxy_error", "code": 400}})

    # ---- forwarding -----------------------------------------------------
    async def forward(self, proxy, request: Request, endpoint: dict, path: str,
                      record: RequestRecord, body: bytes,
                      t0: float) -> Response:
        try:
            payload = json.loads(body) if body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        prompt, system = flatten_messages(payload.get("messages") or [])
        if not prompt:
            return self._reject(
                proxy, record, t0,
                "chat request has no user message content",
                short="no user message to send to the agent")

        # record.model was resolved upstream of here (an allowlisted model the
        # client asked for, else model_override, else the discovered default).
        model, refusal = self._pick_model(
            proxy, endpoint, payload.get("model") or record.model)
        if refusal:
            return self._reject(proxy, record, t0, refusal)
        record.model = model
        turn = {
            "prompt": prompt,
            "system": system,
            "model": split_model(model),
            "agent": payload.get("agent") or endpoint.get("default_agent"),
            "headers": _basic_auth(endpoint.get("upstream_key")),
        }
        if proxy.settings.current.log_bodies:
            record.prompt_body = json.dumps(
                {"system": system, "prompt": prompt})[:_BODY_LIMIT]

        if record.stream:
            return self._stream_response(proxy, endpoint, record, turn, t0)

        status, completion, cost = await self._run_turn(
            proxy, endpoint, record, turn, t0)
        proxy.finalize_adapter_response(
            endpoint, record, t0, status, completion, cost_usd=cost)
        return JSONResponse(status_code=status, content=completion)

    # ---- session lifecycle ----------------------------------------------
    async def _run_turn(self, proxy, endpoint: dict, record: RequestRecord,
                        turn: dict, t0: float) -> tuple[int, dict, float | None]:
        """Create a session, run one turn, delete it. Returns
        (status, OpenAI chat.completion payload, cost_usd or None)."""
        base = endpoint["base_url"]
        headers = turn["headers"]
        http: httpx.AsyncClient = proxy.http

        try:
            created = await http.post(
                f"{base}/session", headers=headers,
                json={"title": f"relay {record.id}"}, timeout=_SETUP_TIMEOUT)
        except httpx.HTTPError as e:
            raise classify_httpx_error(e) from e
        if created.status_code >= 400:
            raise RetryableUpstreamError(
                f"HTTP {created.status_code} creating session: "
                f"{created.text[:300]}", status=created.status_code)
        try:
            session_id = (created.json() or {}).get("id")
        except ValueError:
            session_id = None
        if not session_id:
            raise RetryableUpstreamError(
                "session create returned no id", status=502)

        message: dict = {"parts": [{"type": "text", "text": turn["prompt"]}]}
        if turn["system"]:
            message["system"] = turn["system"]
        if turn["agent"]:
            message["agent"] = turn["agent"]
        if turn["model"]:
            provider_id, model_id = turn["model"]
            message["model"] = {"providerID": provider_id,
                                "modelID": model_id}

        aborted = False
        try:
            # The deadline is enforced here rather than left to the HTTP
            # client: a turn parked on a `permission: "ask"` prompt is an
            # *idle* wait that a read timeout alone would not necessarily
            # break, and there is no human on this side to answer it.
            reply = await asyncio.wait_for(
                http.post(f"{base}/session/{session_id}/message",
                          headers=headers, json=message,
                          timeout=_TURN_TIMEOUT),
                _TURN_TIMEOUT)
        except (TimeoutError, httpx.TimeoutException) as e:
            aborted = True
            raise RetryableUpstreamError(
                f"agent turn exceeded {_TURN_TIMEOUT:.0f}s "
                "(check the OpenCode instance's permission config)",
                status=504) from e
        except httpx.HTTPError as e:
            raise classify_httpx_error(e) from e
        except (asyncio.CancelledError, GeneratorExit):
            aborted = True
            raise
        finally:
            # Shielded: on a client disconnect this task is already cancelled,
            # so a plain `await` here would abandon the session mid-turn — the
            # exact case the abort exists for. Shielding lets teardown finish
            # detached while the cancellation keeps propagating.
            teardown = asyncio.ensure_future(
                self._teardown(http, base, headers, session_id, aborted))
            try:
                await asyncio.shield(teardown)
            except asyncio.CancelledError:
                pass

        if reply.status_code >= 400:
            raise RetryableUpstreamError(
                f"HTTP {reply.status_code}: {reply.text[:300]}",
                status=reply.status_code)
        try:
            data = reply.json() or {}
        except ValueError as e:
            raise RetryableUpstreamError(
                f"non-JSON reply from agent: {e}", status=502) from e

        # An unknown modelID does not 4xx — OpenCode answers 200 with an empty
        # body ({} with no info and no parts), byte-identical to what an
        # invented model id returns. Left alone that becomes a "successful"
        # completion with empty content and zero tokens, which is exactly the
        # shape a typo in OPENCODE_MODELS produces. Fail loudly instead.
        if not data.get("info") and not data.get("parts"):
            raise RetryableUpstreamError(
                f"agent returned an empty turn for model {record.model!r} — "
                "that model id is probably not served by this OpenCode "
                "instance (GET /v1/models lists the ones that are)",
                status=502)

        return self._to_completion(data, record)

    async def _teardown(self, http: httpx.AsyncClient, base: str,
                        headers: dict, session_id: str, aborted: bool) -> None:
        """Abort (only if the turn was cut short) then delete the session.

        The abort matters: without it an agent whose client hung up keeps
        running tools and burning tokens for a reply nobody will read.
        Failures here are logged, never raised — teardown must not mask the
        real outcome of the turn, and it runs detached (see the caller) so an
        exception escaping would surface as an unhandled task error.
        """
        try:
            if aborted:
                await http.post(f"{base}/session/{session_id}/abort",
                                headers=headers, timeout=_SETUP_TIMEOUT)
            await http.delete(f"{base}/session/{session_id}", headers=headers,
                              timeout=_SETUP_TIMEOUT)
        except Exception as e:  # noqa: BLE001 - detached task; never propagate
            log.warning("session teardown failed", extra={"data": {
                "session": session_id, "aborted": aborted,
                "error": str(e)[:200]}})

    # ---- response translation -------------------------------------------
    def _to_completion(self, data: dict, record: RequestRecord
                       ) -> tuple[int, dict, float | None]:
        info = data.get("info") or {}
        text, tools = extract_text(data.get("parts") or [])

        tokens = info.get("tokens") or {}
        prompt_tokens = tokens.get("input")
        completion_tokens = tokens.get("output")
        reasoning = tokens.get("reasoning") or 0
        if completion_tokens is not None and reasoning:
            completion_tokens += reasoning
        record.prompt_tokens = prompt_tokens
        record.completion_tokens = completion_tokens
        record.total_tokens = (
            (prompt_tokens or 0) + (completion_tokens or 0)
            if prompt_tokens is not None or completion_tokens is not None
            else None)

        provider_id = info.get("providerID")
        model_id = info.get("modelID")
        if provider_id and model_id:
            record.model = f"{provider_id}/{model_id}"

        error = info.get("error")
        finish = _finish_reason(info.get("finish"), error)

        usage = {
            "prompt_tokens": prompt_tokens or 0,
            "completion_tokens": completion_tokens or 0,
            "total_tokens": record.total_tokens or 0,
        }
        completion = {
            "id": f"chatcmpl-{record.id}",
            "object": "chat.completion",
            "created": int(record.ts),
            "model": record.model or "opencode",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": finish,
            }],
            "usage": usage,
        }
        if tools:
            completion["relay"] = {"tools_used": tools}
        if error:
            completion["relay"] = {**completion.get("relay", {}),
                                   "agent_error": str(error)[:500]}

        # OpenCode prices the turn itself against the real provider's rates —
        # more accurate than relay's static table, so it wins when present.
        cost = info.get("cost")
        cost_usd = round(float(cost), 6) if isinstance(cost, (int, float)) else None
        return 200, completion, cost_usd

    # ---- synthesized SSE -------------------------------------------------
    def _stream_response(self, proxy, endpoint: dict, record: RequestRecord,
                         turn: dict, t0: float) -> StreamingResponse:
        """Run the blocking turn, then re-emit it as OpenAI SSE.

        Every OpenAI client works against this; the only lie is TTFT, which
        equals total latency because there is nothing earlier to report.
        ``handle()`` hands the request's completion — live tracking and the
        admission slot — to a StreamingResponse, so this generator owns it.
        """
        chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

        async def gen():
            error: str | None = None
            status = 200
            try:
                try:
                    status, completion, cost = await self._run_turn(
                        proxy, endpoint, record, turn, t0)
                except RetryableUpstreamError as e:
                    # Past the response headers already, so the failover path
                    # is closed; the client gets the error inside the stream.
                    status = e.status or 502
                    error = str(e)[:500]
                    record.status = status
                    record.ok = False
                    record.error = error
                    record.latency_ms = (time.perf_counter() - t0) * 1000
                    proxy.telemetry.submit(record)
                    proxy.router.report_failure(endpoint["id"], error)
                    yield _sse({"error": {
                        "message": f"upstream error: {e}",
                        "type": "relay_proxy_error", "code": status}})
                    yield b"data: [DONE]\n\n"
                    return

                record.ttft_ms = (time.perf_counter() - t0) * 1000
                proxy.live.set_ttft(record.id, record.ttft_ms)

                choice = completion["choices"][0]
                text = choice["message"]["content"]
                model = completion["model"]
                base = {"id": chunk_id, "object": "chat.completion.chunk",
                        "created": completion["created"], "model": model}

                yield _sse({**base, "choices": [
                    {"index": 0, "delta": {"role": "assistant"},
                     "finish_reason": None}]})
                if text:
                    yield _sse({**base, "choices": [
                        {"index": 0, "delta": {"content": text},
                         "finish_reason": None}]})
                    proxy.live.bump(record.id, 1)
                yield _sse({**base, "choices": [
                    {"index": 0, "delta": {},
                     "finish_reason": choice["finish_reason"]}]})
                # Usage frame: the same contract inject_stream_usage relies on,
                # so the dashboard reads real counts off a synthesized stream.
                yield _sse({**base, "choices": [],
                            "usage": completion["usage"]})
                yield b"data: [DONE]\n\n"

                if proxy.settings.current.log_bodies:
                    record.completion_body = text[:_BODY_LIMIT]
                proxy.finalize_adapter_response(
                    endpoint, record, t0, status, completion, cost_usd=cost)
            except (GeneratorExit, asyncio.CancelledError):
                record.error = "client disconnected"
                record.ok = False
                record.status = record.status or 499
                record.latency_ms = (time.perf_counter() - t0) * 1000
                proxy.telemetry.submit(record)
                raise
            finally:
                # Releases the admission slot too: the whole agent turn runs
                # inside this generator, so gating only up to the headers
                # would leave it entirely unbounded.
                proxy.finish(record.id)

        return StreamingResponse(gen(), status_code=200,
                                 media_type="text/event-stream")


def _sse(obj: dict) -> bytes:
    return f"data: {json.dumps(obj)}\n\n".encode()
