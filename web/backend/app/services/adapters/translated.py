"""The base for protocols relay translates (anthropic, gemini; GAIFA plan llm-servers 1.4–1.5).

An adapter turns an OpenAI chat request into its provider's, and the provider's
reply into a stream of plain events; this class turns those events into OpenAI
SSE chunks (a streamed request) or one ``chat.completion`` (a plain one), and
keeps relay's bookkeeping the same as for every other protocol: TTFT, the live
view, telemetry, router health.

Events (dicts, key ``t``):
  text {text} · thinking {text} · tool {index, id, name} · tool_args {index, args}
  details {details: [...]} · finish {reason, refusal?} · usage {prompt, completion,
  cached, cache_write, reasoning}

``reasoning_details`` carries what a provider needs back verbatim on the next
turn (Claude's thinking blocks and signatures, Gemini's thought signatures). It
rides on the message (and the stream's last delta); a client that sends the
assistant message back unchanged keeps the conversation valid.

Errors before the first event are HTTP errors with the provider's status
(4xx: the request or the key; 5xx/529 and connection trouble count against the
endpoint's health). After the first byte of a stream they arrive in-band.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator

from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.services.adapters.base import RetryableUpstreamError, UpstreamAdapter
from app.services.telemetry import RequestRecord


class UpstreamError(Exception):
    """The provider answered with an error (``status``) or couldn't be reached (``status`` None)."""

    def __init__(self, status: int | None, message: str, retry_after: str | None = None, kind: str = "upstream_error"):
        super().__init__(message)
        self.status, self.message, self.retry_after, self.kind = status, message, retry_after, kind


def _sse(obj: dict) -> bytes:
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


def _error_body(status: int, message: str, kind: str) -> dict:
    return {"error": {"message": message, "type": kind, "code": status}}


class TranslatedAdapter(UpstreamAdapter):
    """Subclasses implement ``events()`` (and ``probe``)."""

    def supports(self, route: str) -> bool:
        return route == "chat.completions"

    async def events(self, proxy, endpoint: dict, body: dict, record: RequestRecord,
                     dropped: list[str]) -> AsyncIterator[dict]:
        raise NotImplementedError
        yield {}  # pragma: no cover

    # ---- one forwarding attempt ---------------------------------------------
    async def forward(self, proxy, request: Request, endpoint: dict, path: str,
                      record: RequestRecord, body: bytes, t0: float) -> Response:
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            return self._fail(proxy, endpoint, record, t0, UpstreamError(400, "the request body isn't JSON", kind="invalid_request_error"))
        dropped: list[str] = []
        gen = self.events(proxy, endpoint, payload, record, dropped)
        try:
            first = await gen.__anext__()
        except StopAsyncIteration:
            first = None
        except UpstreamError as e:
            if e.status is None or e.status >= 500:
                raise RetryableUpstreamError(e.message, status=e.status or 502) from e
            return self._fail(proxy, endpoint, record, t0, e)
        headers = {"X-Relay-Dropped": ",".join(dropped)} if dropped else {}
        if record.stream:
            return StreamingResponse(self._stream(proxy, endpoint, record, t0, first, gen), status_code=200,
                                     media_type="text/event-stream", headers=headers)
        return await self._complete(proxy, endpoint, record, t0, first, gen, headers)

    def _fail(self, proxy, endpoint: dict, record: RequestRecord, t0: float, e: UpstreamError) -> Response:
        """A 4xx: the request or the key, not the server; health only counts a key problem."""
        status = e.status or 502
        record.status, record.ok, record.error = status, False, e.message[:500]
        record.latency_ms = (time.perf_counter() - t0) * 1000
        proxy.telemetry.submit(record)
        if status in (401, 403):
            proxy.router.report_failure(endpoint["id"], e.message[:200])
        headers = {"Retry-After": e.retry_after} if e.retry_after else {}
        return JSONResponse(_error_body(status, e.message, e.kind), status_code=status, headers=headers)

    # ---- assembling ----------------------------------------------------------
    @staticmethod
    def _usage(ev: dict) -> dict:
        prompt = (ev.get("prompt") or 0) + (ev.get("cached") or 0) + (ev.get("cache_write") or 0)
        u = {"prompt_tokens": prompt, "completion_tokens": ev.get("completion") or 0}
        u["total_tokens"] = u["prompt_tokens"] + u["completion_tokens"]
        u["prompt_tokens_details"] = {"cached_tokens": ev.get("cached") or 0, "cache_creation_tokens": ev.get("cache_write") or 0}
        if ev.get("reasoning"):
            u["completion_tokens_details"] = {"reasoning_tokens": ev["reasoning"]}
        return u

    @staticmethod
    def _record_usage(record: RequestRecord, u: dict) -> None:
        record.prompt_tokens = u["prompt_tokens"]
        record.completion_tokens = u["completion_tokens"]
        record.total_tokens = u["total_tokens"]

    async def _complete(self, proxy, endpoint, record, t0, first, gen, headers) -> Response:
        text, thinking, tools, details = [], [], {}, []
        finish, usage, refusal = "stop", None, None
        evs = [first] if first else []
        try:
            async def all_events():
                for e in evs:
                    yield e
                async for e in gen:
                    yield e
            async for ev in all_events():
                t = ev["t"]
                if t == "text":
                    text.append(ev["text"])
                elif t == "thinking":
                    thinking.append(ev["text"])
                elif t == "tool":
                    tools[ev["index"]] = {"id": ev["id"], "type": "function", "function": {"name": ev["name"], "arguments": ""}}
                elif t == "tool_args":
                    tools[ev["index"]]["function"]["arguments"] += ev["args"]
                elif t == "details":
                    details.extend(ev["details"])
                elif t == "finish":
                    finish, refusal = ev["reason"], ev.get("refusal")
                elif t == "usage":
                    usage = self._usage(ev)
        except UpstreamError as e:
            return self._fail(proxy, endpoint, record, t0, e)
        record.ttft_ms = (time.perf_counter() - t0) * 1000
        message: dict = {"role": "assistant", "content": "".join(text) or (None if tools else "")}
        if thinking:
            message["reasoning_content"] = "".join(thinking)
        if tools:
            message["tool_calls"] = [tools[i] for i in sorted(tools)]
        if details:
            message["reasoning_details"] = details
        if refusal:
            message["refusal"] = refusal
        payload = {"id": f"chatcmpl-{record.id}", "object": "chat.completion", "created": int(record.ts),
                   "model": record.model, "choices": [{"index": 0, "message": message, "finish_reason": finish}]}
        if usage:
            payload["usage"] = usage
            self._record_usage(record, usage)
        proxy.finalize_adapter_response(endpoint, record, t0, 200, payload)
        return JSONResponse(payload, headers=headers)

    def _stream(self, proxy, endpoint, record, t0, first, gen):
        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"

        async def run():
            error = None
            count = 0
            base = {"id": cid, "object": "chat.completion.chunk", "created": int(record.ts), "model": record.model}
            sent_role = False

            def chunk(delta: dict, finish: str | None = None) -> bytes:
                nonlocal sent_role
                if not sent_role:
                    delta = {"role": "assistant", **delta}
                    sent_role = True
                return _sse({**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})

            async def all_events():
                if first:
                    yield first
                async for e in gen:
                    yield e
            usage = None
            texts: list[str] = []
            try:
                async for ev in all_events():
                    t = ev["t"]
                    if record.ttft_ms is None and t in ("text", "thinking", "tool"):
                        record.ttft_ms = (time.perf_counter() - t0) * 1000
                        proxy.live.set_ttft(record.id, record.ttft_ms)
                    count += 1
                    proxy.live.bump(record.id, count)
                    if t == "text":
                        texts.append(ev["text"])
                        yield chunk({"content": ev["text"]})
                    elif t == "thinking":
                        yield chunk({"reasoning_content": ev["text"]})
                    elif t == "tool":
                        yield chunk({"tool_calls": [{"index": ev["index"], "id": ev["id"], "type": "function",
                                                     "function": {"name": ev["name"], "arguments": ""}}]})
                    elif t == "tool_args":
                        yield chunk({"tool_calls": [{"index": ev["index"], "function": {"arguments": ev["args"]}}]})
                    elif t == "details":
                        yield chunk({"reasoning_details": ev["details"]})
                    elif t == "finish":
                        yield chunk({"refusal": ev["refusal"]} if ev.get("refusal") else {}, ev["reason"])
                    elif t == "usage":
                        usage = self._usage(ev)
                        self._record_usage(record, usage)
                        yield _sse({**base, "choices": [], "usage": usage})
                yield b"data: [DONE]\n\n"
            except UpstreamError as e:
                error = e.message[:500]
                record.status = e.status or 502
                yield _sse(_error_body(e.status or 502, e.message, e.kind))
                yield b"data: [DONE]\n\n"
            except BaseException as e:  # client gone (GeneratorExit / CancelledError) and anything else
                error = error or f"{type(e).__name__}: {e}" if not isinstance(e, GeneratorExit) else "client disconnected"
                raise
            finally:
                await gen.aclose()
                proxy.finish(record.id)
                if proxy.settings.current.log_bodies and texts:
                    record.completion_body = json.dumps([{"role": "assistant", "content": "".join(texts)}])[:100_000]  # proxy._BODY_LIMIT
                record.error = error
                status = record.status if error and record.status else (200 if not error else 502)
                if not error:
                    gen_ms = (time.perf_counter() - t0) * 1000 - (record.ttft_ms or 0)
                    if record.completion_tokens and gen_ms > 0:
                        record.tokens_per_sec = round(record.completion_tokens / (gen_ms / 1000), 2)
                proxy.finalize_adapter_response(endpoint, record, t0, status,
                                                {"choices": [{"message": {"role": "assistant"}}]})

        return run()
