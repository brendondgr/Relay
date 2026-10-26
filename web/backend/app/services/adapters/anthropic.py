"""Claude's Messages API behind relay's OpenAI ``/v1/chat/completions`` (GAIFA plan llm-servers 1.4).

Through the official ``anthropic`` SDK (2.x: API version 2026-10-21), always
streamed upstream, so a plain request is the same call assembled. What it
translates:

- **Request:** ``system`` (and ``developer``) turns before the first other turn
  become the top-level ``system``; later ones stay mid-conversation system
  messages. Images (data URIs or URLs) and PDF ``file`` parts become ``image``
  and ``document`` blocks; an assistant's ``tool_calls`` become ``tool_use``;
  ``tool`` messages become ``tool_result`` blocks in one user turn. An
  assistant message's ``reasoning_details`` (thinking blocks this adapter
  returned) go back first and unchanged; the API drops, unbilled, any a model
  can't read. ``max_tokens`` (which Claude requires and GAIFA's agent never
  sends) is the model's own output cap from its live listing. Thinking is
  adaptive with a readable summary where the model lists adaptive thinking;
  ``reasoning_effort`` becomes ``output_config.effort``, snapped to the ladder
  the model lists. The prompt is cached (top-level ``cache_control``).
  ``temperature``, ``top_p`` and ``top_k`` don't exist on this API version and
  are dropped (``X-Relay-Dropped``).
- **Things some models refuse** (forced ``tool_choice``, mid-conversation
  system messages, the server-side refusal fallback, a thinking or effort
  setting) are sent, and on that model's 400 naming them, dropped and the call
  made once more: no table of which model takes what.
- **Reply:** text, thinking (as ``reasoning_content``), tool calls with their
  arguments streamed, ``reasoning_details`` (the thinking blocks and their
  signatures, for the next turn), ``stop_reason`` → ``finish_reason``
  (``refusal`` → ``content_filter`` with ``refusal`` set), and usage with the
  cache reads and writes.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator

import httpx

from app.core.logging import get_logger
from app.schemas import EndpointTestResult
from app.services.adapters.translated import TranslatedAdapter, UpstreamError
from app.services.telemetry import RequestRecord

log = get_logger("anthropic")

FALLBACK_BETA = "server-side-fallback-2026-07-01"
DEFAULT_MAX_TOKENS = 32000
STOP = {"end_turn": "stop", "stop_sequence": "stop", "pause_turn": "stop", "max_tokens": "length",
        "tool_use": "tool_calls", "refusal": "content_filter", "model_context_window_exceeded": "length"}
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DATA_URI = re.compile(r"^data:([^;,]+);base64,(.*)$", re.S)


def _client(endpoint: dict):
    import anthropic
    return anthropic.AsyncAnthropic(api_key=endpoint.get("upstream_key") or "", base_url=endpoint["base_url"],
                                    max_retries=0, timeout=anthropic.Timeout(1800.0, connect=10.0))


def _snap_effort(want: str, ladder: list[str] | None) -> str | None:
    want = {"minimal": "low", "none": "low"}.get(want, want)
    if want not in EFFORTS:
        return None
    if not ladder:
        return want
    if want in ladder:
        return want
    i = EFFORTS.index(want)
    below = [e for e in ladder if EFFORTS.index(e) <= i]
    return below[-1] if below else ladder[0]


def _media(url: str) -> dict:
    m = DATA_URI.match(url or "")
    if m:
        return {"type": "base64", "media_type": m.group(1), "data": m.group(2)}
    return {"type": "url", "url": url}


def _content(parts) -> list[dict]:
    """An OpenAI message's content → Claude blocks."""
    if parts is None:
        return []
    if isinstance(parts, str):
        return [{"type": "text", "text": parts}] if parts else []
    out = []
    for p in parts:
        kind = p.get("type")
        if kind == "text" and p.get("text"):
            out.append({"type": "text", "text": p["text"]})
        elif kind == "image_url":
            url = p["image_url"]["url"] if isinstance(p.get("image_url"), dict) else p.get("image_url")
            out.append({"type": "image", "source": _media(url)})
        elif kind == "file":
            f = p.get("file") or {}
            data = f.get("file_data") or ""
            src = _media(data)
            if src["type"] == "base64" and src["media_type"] == "application/pdf":
                block = {"type": "document", "source": src}
                if f.get("filename"):
                    block["title"] = f["filename"]
                out.append(block)
            else:
                out.append({"type": "text", "text": f"[file {f.get('filename') or ''}: this model reads PDFs and images only]"})
        elif kind == "input_audio":
            out.append({"type": "text", "text": "[audio: this model doesn't take audio]"})
    return out


def _details(message: dict) -> list[dict]:
    return [d["block"] for d in message.get("reasoning_details") or []
            if isinstance(d, dict) and d.get("format") == "anthropic" and isinstance(d.get("block"), dict)]


def translate(body: dict) -> dict:
    """The OpenAI request → the Messages API's (without the model-dependent parts)."""
    system: list[str] = []
    messages: list[dict] = []
    seen_other = False
    pending_results: list[dict] = []

    def flush_results():
        if pending_results:
            messages.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()

    for m in body.get("messages") or []:
        role = m.get("role")
        if role in ("system", "developer"):
            text = m["content"] if isinstance(m.get("content"), str) else "".join(
                p.get("text", "") for p in m.get("content") or [] if p.get("type") == "text")
            if not seen_other:
                system.append(text)
            else:
                flush_results()
                messages.append({"role": "system", "content": text})
            continue
        seen_other = True
        if role == "tool":
            content = m.get("content")
            blocks = [b for b in _content(content)] if isinstance(content, list) else content or ""
            pending_results.append({"type": "tool_result", "tool_use_id": m.get("tool_call_id") or "", "content": blocks})
            continue
        flush_results()
        if role == "assistant":
            blocks = list(_details(m)) + _content(m.get("content"))
            for call in m.get("tool_calls") or []:
                fn = call.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {"_raw": fn.get("arguments")}
                blocks.append({"type": "tool_use", "id": call.get("id") or "", "name": fn.get("name") or "",
                               "input": args if isinstance(args, dict) else {"value": args}})
            if blocks:
                messages.append({"role": "assistant", "content": blocks})
        else:
            messages.append({"role": "user", "content": _content(m.get("content")) or [{"type": "text", "text": ""}]})
    flush_results()
    params: dict = {"messages": messages}
    if system:
        params["system"] = "\n\n".join(s for s in system if s)
    tools = []
    for t in body.get("tools") or []:
        fn = t.get("function") if t.get("type") == "function" else None
        if fn:
            tools.append({"name": fn["name"], "description": fn.get("description") or "",
                          "input_schema": fn.get("parameters") or {"type": "object", "properties": {}}})
    if tools:
        params["tools"] = tools
        choice = body.get("tool_choice")
        tc: dict = {"type": "auto"}
        if choice == "none":
            tc = {"type": "none"}
        elif choice == "required":
            tc = {"type": "any"}
        elif isinstance(choice, dict) and (choice.get("function") or {}).get("name"):
            tc = {"type": "tool", "name": choice["function"]["name"]}
        if body.get("parallel_tool_calls") is False and tc["type"] != "none":
            tc["disable_parallel_tool_use"] = True
        params["tool_choice"] = tc
    stop = body.get("stop")
    if stop:
        params["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)
    fmt = body.get("response_format") or {}
    if fmt.get("type") == "json_schema" and (fmt.get("json_schema") or {}).get("schema"):
        params["output_config"] = {"format": {"type": "json_schema", "schema": fmt["json_schema"]["schema"]}}
    return params


# What a 400 names → how to take it out of the request (applied once each).
def _drop(params: dict, extra: dict, message: str) -> str | None:
    low = message.lower()
    tc = params.get("tool_choice") or {}
    if "tool_choice" in low and tc.get("type") in ("any", "tool"):
        params["tool_choice"] = {"type": "auto", **({"disable_parallel_tool_use": True} if tc.get("disable_parallel_tool_use") else {})}
        return "tool_choice"
    if "system" in low and any(m["role"] == "system" for m in params["messages"]):
        lifted = [m["content"] for m in params["messages"] if m["role"] == "system"]
        params["messages"] = [m for m in params["messages"] if m["role"] != "system"]
        params["system"] = "\n\n".join([params.get("system", "")] + lifted).strip()
        return "system_messages"
    if "fallback" in low and "fallbacks" in extra.get("body", {}):
        extra["body"].pop("fallbacks")
        extra["headers"].pop("anthropic-beta", None)
        return "fallbacks"
    if "thinking" in low and "thinking" in params:
        params.pop("thinking")
        return "thinking"
    if ("effort" in low or "output_config" in low) and (params.get("output_config") or {}).get("effort"):
        params["output_config"].pop("effort")
        if not params["output_config"]:
            params.pop("output_config")
        return "effort"
    if "cache_control" in low and "cache_control" in params:
        params.pop("cache_control")
        return "cache_control"
    return None


def _error(e) -> UpstreamError:
    import anthropic
    if isinstance(e, anthropic.APIStatusError):
        body = e.body if isinstance(e.body, dict) else {}
        err = body.get("error") if isinstance(body.get("error"), dict) else {}
        retry = e.response.headers.get("retry-after") if e.response is not None else None
        return UpstreamError(e.status_code, err.get("message") or e.message or str(e), retry, err.get("type") or "upstream_error")
    if isinstance(e, anthropic.APIError):
        return UpstreamError(None, f"{type(e).__name__}: {e}")
    return UpstreamError(None, f"{type(e).__name__}: {e}")


class AnthropicAdapter(TranslatedAdapter):
    name = "anthropic"

    async def probe(self, http: httpx.AsyncClient, endpoint: dict, timeout: float) -> EndpointTestResult:
        import time

        import anthropic
        client = anthropic.AsyncAnthropic(api_key=endpoint.get("upstream_key") or "", base_url=endpoint["base_url"],
                                          max_retries=0, timeout=anthropic.Timeout(timeout, connect=min(timeout, 5.0)))
        t0 = time.perf_counter()
        try:
            page = await client.models.list(limit=20)
            ids = [m.id for m in page.data]
            return EndpointTestResult(ok=True, latency_ms=round((time.perf_counter() - t0) * 1000, 1), models=ids)
        except anthropic.APIError as e:
            return EndpointTestResult(ok=False, latency_ms=round((time.perf_counter() - t0) * 1000, 1), error=_error(e).message[:300])
        finally:
            await client.close()

    def _params(self, proxy, endpoint: dict, body: dict, record: RequestRecord, dropped: list[str]) -> tuple[dict, dict]:
        params = translate(body)
        params["model"] = body.get("model") or record.model or ""
        listed = proxy.discovery.find(endpoint["id"], params["model"]) if proxy.discovery else None
        cap = listed.max_output if listed and listed.max_output else DEFAULT_MAX_TOKENS
        want = body.get("max_completion_tokens") or body.get("max_tokens")
        params["max_tokens"] = min(int(want), cap) if want else cap
        if listed and listed.reasoning and "adaptive" in (listed.thinking_types or []):
            params["thinking"] = {"type": "adaptive", "display": "summarized"}
        effort = body.get("reasoning_effort")
        if effort:
            snapped = _snap_effort(str(effort), listed.effort if listed else None)
            if snapped:
                params.setdefault("output_config", {})["effort"] = snapped
        for k in ("temperature", "top_p", "top_k", "presence_penalty", "frequency_penalty", "seed", "logit_bias", "n"):
            if body.get(k) is not None:
                dropped.append(k)
        if getattr(proxy.settings.current, "prompt_cache", True):
            params["cache_control"] = {"type": "ephemeral"}
        extra = {"headers": {"anthropic-beta": FALLBACK_BETA}, "body": {"fallbacks": "default"}}
        return params, extra

    async def events(self, proxy, endpoint: dict, body: dict, record: RequestRecord,
                     dropped: list[str]) -> AsyncIterator[dict]:
        import anthropic
        params, extra = self._params(proxy, endpoint, body, record, dropped)
        client = _client(endpoint)
        try:
            stream = None
            for _ in range(6):
                try:
                    stream = await client.messages.create(**params, stream=True, extra_headers=extra["headers"] or None,
                                                          extra_body=extra["body"] or None)
                    break
                except anthropic.BadRequestError as e:
                    what = _drop(params, extra, _error(e).message)
                    if not what:
                        raise _error(e) from e
                    if what not in ("fallbacks",):
                        dropped.append(what)
                    log.info("retrying without what the model refused", extra={"data": {"model": params["model"], "dropped": what}})
                except anthropic.APIError as e:
                    raise _error(e) from e
            if stream is None:
                raise UpstreamError(400, "the model refused the request")
            async for ev in self._translate(stream, record):
                yield ev
        finally:
            await client.close()

    async def _translate(self, stream, record: RequestRecord) -> AsyncIterator[dict]:
        import anthropic
        blocks: dict[int, dict] = {}            # stream index → the block being built
        tool_index: dict[int, int] = {}
        details: list[dict] = []
        usage = {"prompt": 0, "completion": 0, "cached": 0, "cache_write": 0}
        stop, refusal = "end_turn", None
        try:
            async for ev in stream:
                t = ev.type
                if t == "message_start":
                    u = ev.message.usage
                    usage.update(prompt=u.input_tokens or 0, cached=getattr(u, "cache_read_input_tokens", 0) or 0,
                                 cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0)
                    if getattr(ev.message, "model", None):
                        record.model = ev.message.model
                elif t == "content_block_start":
                    b = ev.content_block
                    if b.type == "thinking":
                        blocks[ev.index] = {"type": "thinking", "thinking": getattr(b, "thinking", "") or "", "signature": getattr(b, "signature", "") or ""}
                    elif b.type == "redacted_thinking":
                        blocks[ev.index] = {"type": "redacted_thinking", "data": b.data}
                    elif b.type == "tool_use":
                        i = len(tool_index)
                        tool_index[ev.index] = i
                        yield {"t": "tool", "index": i, "id": b.id, "name": b.name}
                    elif b.type == "text" and getattr(b, "text", ""):
                        yield {"t": "text", "text": b.text}
                elif t == "content_block_delta":
                    d = ev.delta
                    if d.type == "text_delta":
                        yield {"t": "text", "text": d.text}
                    elif d.type == "thinking_delta":
                        blocks.setdefault(ev.index, {"type": "thinking", "thinking": "", "signature": ""})["thinking"] += d.thinking
                        if d.thinking:
                            yield {"t": "thinking", "text": d.thinking}
                    elif d.type == "signature_delta":
                        blocks.setdefault(ev.index, {"type": "thinking", "thinking": "", "signature": ""})["signature"] = d.signature
                    elif d.type == "input_json_delta" and ev.index in tool_index:
                        if d.partial_json:
                            yield {"t": "tool_args", "index": tool_index[ev.index], "args": d.partial_json}
                elif t == "content_block_stop":
                    b = blocks.pop(ev.index, None)
                    if b is not None:
                        details.append({"format": "anthropic", "model": record.model, "block": b})
                elif t == "message_delta":
                    stop = ev.delta.stop_reason or stop
                    sd = getattr(ev.delta, "stop_details", None)
                    if stop == "refusal":
                        refusal = (getattr(sd, "explanation", None) or "The model declined this request.") if sd else "The model declined this request."
                    if ev.usage and ev.usage.output_tokens is not None:
                        usage["completion"] = ev.usage.output_tokens
        except anthropic.APIError as e:
            raise _error(e) from e
        if details:
            yield {"t": "details", "details": details}
        yield {"t": "finish", "reason": STOP.get(stop, "stop"), **({"refusal": refusal} if refusal else {})}
        yield {"t": "usage", **usage}
