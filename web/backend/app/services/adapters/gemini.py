"""Gemini behind relay's OpenAI ``/v1/chat/completions`` (GAIFA plan llm-servers 1.5).

Through the official ``google-genai`` SDK (``client.aio.models.generate_content_stream``),
always streamed upstream. What it translates:

- **Request:** system turns become ``system_instruction``; user turns' text,
  images (data URIs inline, URLs as file data) and PDFs become parts; an
  assistant turn becomes a ``model`` turn: the very parts Gemini returned for it
  when its ``reasoning_details`` carry them (thought signatures included, which
  Gemini 3 requires on the next request after a function call), else rebuilt
  from its text and tool calls. ``tool`` messages become ``function_response``
  parts in one user turn, named after the call they answer. Tools are function
  declarations with their JSON Schema as given (``parameters_json_schema``, so
  nothing is silently cut down to Gemini's older schema subset); ``tool_choice``
  becomes the function-calling mode. Sampling parameters pass through (Gemini
  takes them). ``reasoning_effort`` becomes ``thinking_level`` (and, on a model
  whose 400 says it wants a budget, ``thinking_budget``); thought summaries are
  asked for where the model thinks.
- **Reply:** text, thought summaries (as ``reasoning_content``), function calls
  (whole, as one tool-call delta each), ``reasoning_details`` (the model's parts,
  for the next turn), the finish reason, and usage with cached and thinking
  tokens. A blocked prompt (a 200 with no candidates) or a safety stop is
  ``content_filter`` with ``refusal`` saying why, never an empty reply.
"""

from __future__ import annotations

import base64
import itertools
import json
import mimetypes
import re
import time
from collections.abc import AsyncIterator

import httpx

from app.core.logging import get_logger
from app.schemas import EndpointTestResult
from app.services.adapters.translated import TranslatedAdapter, UpstreamError
from app.services.telemetry import RequestRecord

log = get_logger("gemini")

DATA_URI = re.compile(r"^data:([^;,]+);base64,(.*)$", re.S)
BLOCKED = {"SAFETY", "PROHIBITED_CONTENT", "RECITATION", "BLOCKLIST", "SPII", "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT"}
LEVEL = {"minimal": "minimal", "none": "minimal", "low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}
BUDGET = {"minimal": 512, "low": 2048, "medium": 8192, "high": 24576}


def _client(endpoint: dict, timeout_ms: int = 1_800_000):
    from google import genai
    from google.genai import types
    return genai.Client(api_key=endpoint.get("upstream_key") or "",
                        http_options=types.HttpOptions(base_url=endpoint["base_url"], timeout=timeout_ms))


def _media_part(url: str, mime: str | None = None) -> dict:
    m = DATA_URI.match(url or "")
    if m:
        return {"inline_data": {"mime_type": m.group(1), "data": base64.b64decode(m.group(2))}}
    return {"file_data": {"file_uri": url, "mime_type": mime or mimetypes.guess_type(url)[0] or "image/jpeg"}}


def _parts(content) -> list[dict]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"text": content}] if content else []
    out = []
    for p in content:
        kind = p.get("type")
        if kind == "text" and p.get("text"):
            out.append({"text": p["text"]})
        elif kind == "image_url":
            url = p["image_url"]["url"] if isinstance(p.get("image_url"), dict) else p.get("image_url")
            out.append(_media_part(url))
        elif kind == "file":
            f = p.get("file") or {}
            if f.get("file_data"):
                out.append(_media_part(f["file_data"]))
        elif kind == "input_audio":
            a = p.get("input_audio") or {}
            if a.get("data"):
                out.append({"inline_data": {"mime_type": f"audio/{a.get('format') or 'wav'}", "data": base64.b64decode(a["data"])}})
    return out


def _saved_parts(message: dict) -> list[dict] | None:
    for d in message.get("reasoning_details") or []:
        if isinstance(d, dict) and d.get("format") == "gemini" and isinstance(d.get("parts"), list):
            return d["parts"]
    return None


def translate(body: dict) -> tuple[list[dict], dict]:
    """The OpenAI request → (contents, config) for generate_content (without the model-dependent parts)."""
    system: list[str] = []
    contents: list[dict] = []
    names: dict[str, str] = {}              # tool call id → function name (a function_response needs it)
    results: list[dict] = []

    def flush():
        if results:
            contents.append({"role": "user", "parts": list(results)})
            results.clear()

    for m in body.get("messages") or []:
        role = m.get("role")
        if role in ("system", "developer"):
            c = m.get("content")
            system.append(c if isinstance(c, str) else "".join(p.get("text", "") for p in c or [] if p.get("type") == "text"))
            continue
        if role == "tool":
            c = m.get("content")
            text = c if isinstance(c, str) else "".join(p.get("text", "") for p in c or [] if p.get("type") == "text")
            try:
                response = json.loads(text)
                if not isinstance(response, dict):
                    response = {"result": response}
            except ValueError:
                response = {"result": text}
            cid = m.get("tool_call_id") or ""
            results.append({"function_response": {"name": names.get(cid, m.get("name") or "tool"), "response": response,
                                                  **({"id": cid} if cid else {})}})
            continue
        flush()
        if role == "assistant":
            for call in m.get("tool_calls") or []:
                names[call.get("id") or ""] = (call.get("function") or {}).get("name") or ""
            saved = _saved_parts(m)
            if saved is not None:
                parts = saved
            else:
                parts = _parts(m.get("content"))
                for call in m.get("tool_calls") or []:
                    fn = call.get("function") or {}
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    parts.append({"function_call": {"name": fn.get("name") or "", "args": args if isinstance(args, dict) else {},
                                                    **({"id": call["id"]} if call.get("id") else {})}})
            if parts:
                contents.append({"role": "model", "parts": parts})
        else:
            contents.append({"role": "user", "parts": _parts(m.get("content")) or [{"text": ""}]})
    flush()

    config: dict = {"automatic_function_calling": {"disable": True}}
    if system:
        config["system_instruction"] = "\n\n".join(s for s in system if s)
    decls = []
    for t in body.get("tools") or []:
        fn = t.get("function") if t.get("type") == "function" else None
        if fn:
            decls.append({"name": fn["name"], "description": fn.get("description") or "",
                          "parameters_json_schema": fn.get("parameters") or {"type": "object", "properties": {}}})
    if decls:
        config["tools"] = [{"function_declarations": decls}]
        choice = body.get("tool_choice")
        mode = {"none": "NONE", "required": "ANY"}.get(choice if isinstance(choice, str) else "", "AUTO")
        fcc: dict = {"mode": mode}
        if isinstance(choice, dict) and (choice.get("function") or {}).get("name"):
            fcc = {"mode": "ANY", "allowed_function_names": [choice["function"]["name"]]}
        config["tool_config"] = {"function_calling_config": fcc}
    for src, dst in (("temperature", "temperature"), ("top_p", "top_p"), ("seed", "seed"),
                     ("presence_penalty", "presence_penalty"), ("frequency_penalty", "frequency_penalty")):
        if body.get(src) is not None:
            config[dst] = body[src]
    want = body.get("max_completion_tokens") or body.get("max_tokens")
    if want:
        config["max_output_tokens"] = int(want)
    stop = body.get("stop")
    if stop:
        config["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)
    fmt = body.get("response_format") or {}
    if fmt.get("type") == "json_schema" and (fmt.get("json_schema") or {}).get("schema"):
        config["response_mime_type"] = "application/json"
        config["response_json_schema"] = fmt["json_schema"]["schema"]
    elif fmt.get("type") == "json_object":
        config["response_mime_type"] = "application/json"
    return contents, config


def _error(e) -> UpstreamError:
    from google.genai import errors
    if isinstance(e, errors.APIError):
        return UpstreamError(getattr(e, "code", None) or 502, getattr(e, "message", None) or str(e),
                             kind=(getattr(e, "status", None) or "upstream_error").lower())
    return UpstreamError(None, f"{type(e).__name__}: {e}")


def _dump(part) -> dict:
    return part.model_dump(exclude_none=True, mode="json")


def _merge(parts: list[dict]) -> list[dict]:
    """Streamed parts → the model's turn: adjacent text pieces of the same kind (thought or not) joined;
    a part carrying a signature or anything but text is kept as it came."""
    out: list[dict] = []
    for p in parts:
        prev = out[-1] if out else None
        if (prev is not None and set(p) <= {"text", "thought"} and set(prev) <= {"text", "thought"}
                and bool(p.get("thought")) == bool(prev.get("thought"))):
            prev["text"] = prev.get("text", "") + p.get("text", "")
        else:
            out.append(dict(p))
    return out


class GeminiAdapter(TranslatedAdapter):
    name = "gemini"

    async def probe(self, http: httpx.AsyncClient, endpoint: dict, timeout: float) -> EndpointTestResult:
        t0 = time.perf_counter()
        client = _client(endpoint, int(timeout * 1000))
        try:
            ids = []
            async for m in await client.aio.models.list(config={"page_size": 50}):
                ids.append((m.name or "").removeprefix("models/"))
                if len(ids) >= 50:
                    break
            return EndpointTestResult(ok=True, latency_ms=round((time.perf_counter() - t0) * 1000, 1), models=ids)
        except Exception as e:  # noqa: BLE001 — a probe reports, never raises
            return EndpointTestResult(ok=False, latency_ms=round((time.perf_counter() - t0) * 1000, 1), error=_error(e).message[:300])

    def _config(self, proxy, endpoint: dict, body: dict, dropped: list[str]) -> tuple[str, list, dict]:
        contents, config = translate(body)
        model = body.get("model") or ""
        listed = proxy.discovery.find(endpoint["id"], model) if proxy.discovery else None
        thinks = listed.reasoning if listed else None
        effort = body.get("reasoning_effort")
        if thinks or effort:
            tc: dict = {"include_thoughts": True}
            if effort and LEVEL.get(str(effort)):
                tc["thinking_level"] = LEVEL[str(effort)]
            config["thinking_config"] = tc
        for k in ("logit_bias", "n"):
            if body.get(k) is not None:
                dropped.append(k)
        if body.get("top_k") is not None:
            config["top_k"] = body["top_k"]
        return model, contents, config

    async def events(self, proxy, endpoint: dict, body: dict, record: RequestRecord,
                     dropped: list[str]) -> AsyncIterator[dict]:
        from google.genai import errors
        model, contents, config = self._config(proxy, endpoint, body, dropped)
        client = _client(endpoint)
        stream = None
        first = None
        for _ in range(3):
            try:
                # The SDK opens the stream on the first read, so a refusal (400) surfaces there: read it here.
                it = (await client.aio.models.generate_content_stream(model=model, contents=contents, config=config)).__aiter__()
                try:
                    first = await it.__anext__()
                except StopAsyncIteration:
                    first = None
                stream = it
                break
            except errors.ClientError as e:
                msg = (getattr(e, "message", "") or str(e)).lower()
                tc = config.get("thinking_config") or {}
                if "thinking_level" in msg and "thinking_level" in tc:
                    level = tc.pop("thinking_level")
                    tc["thinking_budget"] = BUDGET.get(level, 8192)
                    continue
                if "thinking" in msg and tc:
                    config.pop("thinking_config")
                    dropped.append("thinking")
                    continue
                raise _error(e) from e
            except Exception as e:  # noqa: BLE001 — network, server
                raise _error(e) from e
        if stream is None:
            raise UpstreamError(400, "the model refused the request")

        async def responses():
            if first is not None:
                yield first
            async for r in stream:
                yield r
        async for ev in self._translate(responses(), record):
            yield ev

    async def _translate(self, stream, record: RequestRecord) -> AsyncIterator[dict]:
        parts: list[dict] = []
        n_calls = itertools.count()
        calls = 0
        finish, refusal, usage, anything = None, None, None, False
        try:
            async for resp in stream:
                if getattr(resp, "model_version", None):
                    record.model = resp.model_version
                if resp.usage_metadata:
                    u = resp.usage_metadata
                    cached = u.cached_content_token_count or 0
                    usage = {"prompt": (u.prompt_token_count or 0) - cached, "cached": cached, "cache_write": 0,
                             "completion": (u.candidates_token_count or 0) + (u.thoughts_token_count or 0),
                             "reasoning": u.thoughts_token_count or 0}
                if not resp.candidates:
                    fb = resp.prompt_feedback
                    if fb and fb.block_reason:
                        finish = "content_filter"
                        refusal = f"Gemini blocked the prompt ({fb.block_reason}{': ' + fb.block_reason_message if getattr(fb, 'block_reason_message', None) else ''})."
                    continue
                cand = resp.candidates[0]
                for p in (cand.content.parts if cand.content and cand.content.parts else []):
                    parts.append(_dump(p))
                    if p.function_call:
                        i = next(n_calls)
                        calls += 1
                        fc = p.function_call
                        yield {"t": "tool", "index": i, "id": fc.id or f"call_{record.id}_{i}", "name": fc.name or ""}
                        yield {"t": "tool_args", "index": i, "args": json.dumps(fc.args or {})}
                        anything = True
                    elif p.text:
                        anything = True
                        yield {"t": "thinking" if p.thought else "text", "text": p.text}
                if cand.finish_reason:
                    reason = str(getattr(cand.finish_reason, "value", cand.finish_reason))
                    if reason in BLOCKED:
                        finish, refusal = "content_filter", f"Gemini stopped: {reason}."
                    elif reason == "MAX_TOKENS":
                        finish = "length"
                    elif reason == "MALFORMED_FUNCTION_CALL":
                        finish, refusal = "stop", "The model produced a malformed function call."
                    else:
                        finish = "tool_calls" if calls else "stop"
        except Exception as e:  # noqa: BLE001
            raise _error(e) from e
        if not anything and finish is None:
            finish = "stop"
        if parts:
            yield {"t": "details", "details": [{"format": "gemini", "model": record.model, "parts": _merge(parts)}]}
        yield {"t": "finish", "reason": finish or ("tool_calls" if calls else "stop"), **({"refusal": refusal} if refusal else {})}
        if usage:
            yield {"t": "usage", **usage}
