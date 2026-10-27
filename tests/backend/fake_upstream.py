"""A fake OpenAI-compatible upstream + a routing httpx transport.

Hosts:
- ``good``     -> in-process FastAPI app (models, chat/completions incl. SSE)
- ``strict``   -> serves one model, 404s any other (stale-override healing)
- ``opencode`` -> fake ``opencode serve`` (sessions, parts, Basic auth)
- ``flaky``    -> always HTTP 500
- ``extra``    -> any other fake by host name (model facts: llama.cpp
  ``/props``, vLLM ``max_model_len``, a relay hiding vLLM)
- anything else -> httpx.ConnectError (a dead box)
"""

import asyncio
import json

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


def make_upstream() -> tuple[FastAPI, dict]:
    up = FastAPI()
    calls = {"chat": 0, "models": 0, "saw_include_usage": False,
             "last_model": None}

    @up.get("/v1/models")
    async def models():
        calls["models"] += 1
        return {"data": [{"id": "fake-model-7b"}]}

    @up.post("/v1/chat/completions")
    async def chat(request: Request):
        calls["chat"] += 1
        body = await request.json()
        calls["last_model"] = body.get("model")
        if body.get("stream"):
            include_usage = bool(
                (body.get("stream_options") or {}).get("include_usage"))
            calls["saw_include_usage"] = include_usage

            async def gen():
                for i in range(5):
                    chunk = {
                        "id": "c1", "object": "chat.completion.chunk",
                        "model": "fake-model-7b",
                        "choices": [{"index": 0,
                                     "delta": {"content": f"tok{i} "}}],
                    }
                    yield f"data: {json.dumps(chunk)}\n\n"
                    await asyncio.sleep(0.005)
                if include_usage:
                    tail = {"id": "c1", "choices": [],
                            "usage": {"prompt_tokens": 7,
                                      "completion_tokens": 5,
                                      "total_tokens": 12}}
                    yield f"data: {json.dumps(tail)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(gen(), media_type="text/event-stream")
        return {
            "id": "cmpl-1", "object": "chat.completion",
            "model": "fake-model-7b",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "hello"}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3,
                      "total_tokens": 10},
        }

    return up, calls


def make_strict_upstream() -> FastAPI:
    """Serves exactly one model and rejects any other id with an OpenAI-style
    ``model does not exist`` 404 — exercises the proxy's stale-override heal
    (a port whose model was swapped out under a pinned ``model_override``)."""
    up = FastAPI()
    served = "fake-model-7b"

    @up.get("/v1/models")
    async def models():
        return {"data": [{"id": served}]}

    @up.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        if body.get("model") != served:
            return JSONResponse(status_code=404, content={"error": {
                "message": f"The model `{body.get('model')}` does not exist.",
                "type": "invalid_request_error", "code": "model_not_found"}})
        return {
            "id": "cmpl-strict", "object": "chat.completion", "model": served,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2,
                      "total_tokens": 6},
        }

    return up


def make_opencode_upstream() -> tuple[FastAPI, dict]:
    """A fake ``opencode serve``: Basic auth, provider catalog, and the
    session -> message -> delete lifecycle the adapter drives.

    Shapes are copied from a live ``opencode serve`` 1.17.13 (``{info, parts}``
    with usage on ``info.tokens``, a pre-priced ``info.cost``, an ``info.finish``
    reason, and step-start/step-finish parts around the content), so the
    adapter's translation is exercised against reality rather than a convenient
    stand-in for it.
    """
    up = FastAPI()
    calls = {
        "auth": None, "health": 0, "providers": 0, "created": 0,
        "deleted": 0, "aborted": 0, "messages": 0, "last_message": None,
        "open_sessions": set(), "hang": False, "empty_turn": False,
    }

    @up.get("/global/health")
    async def health(request: Request):
        calls["health"] += 1
        calls["auth"] = request.headers.get("authorization")
        return {"healthy": True, "version": "1.14.42"}

    @up.get("/config/providers")
    async def providers(request: Request):
        calls["providers"] += 1
        calls["auth"] = request.headers.get("authorization")
        # Shaped like a real OpenCode Zen catalog: mostly paid, a couple of
        # models with "free" in the id, and a *paid* server default. relay
        # serves only the free ones, so the filter has something to bite on.
        return {
            "providers": [{
                "id": "opencode", "name": "OpenCode Zen",
                "models": {"claude-opus-5": {}, "hy3-free": {
                    "name": "Hy3 (free)", "tool_call": True, "reasoning": True,
                    "limit": {"context": 128000, "output": 8192},
                    "modalities": {"input": ["text", "image"], "output": ["text"]}},
                           "mimo-v2.5-free": {}},
            }, {
                "id": "openai", "name": "OpenAI", "models": {"gpt-5": {}},
            }],
            "default": {"opencode": "claude-opus-5"},
        }

    @up.post("/session")
    async def create_session(request: Request):
        calls["created"] += 1
        calls["auth"] = request.headers.get("authorization")
        sid = f"ses_{calls['created']}"
        calls["open_sessions"].add(sid)
        return {"id": sid, "title": (await request.json()).get("title")}

    @up.post("/session/{sid}/message")
    async def message(sid: str, request: Request):
        calls["messages"] += 1
        body = await request.json()
        calls["last_message"] = {"session": sid, **body}
        if calls["hang"]:
            await asyncio.sleep(30)
        if calls["empty_turn"]:
            # What the real server returns for a modelID it does not serve:
            # 200 with an empty object, no error anywhere.
            return {}
        model = body.get("model") or {}
        return {
            "info": {
                "id": "msg_1", "role": "assistant", "sessionID": sid,
                "providerID": model.get("providerID", "opencode"),
                "modelID": model.get("modelID", "hy3-free"),
                "cost": 0.00123,
                "finish": "stop",
                "tokens": {"total": 17, "input": 11, "output": 4,
                           "reasoning": 2, "cache": {"read": 0, "write": 0}},
                "time": {"created": 1786467497345, "completed": 1786467498301},
            },
            # Part sequence observed from opencode 1.17.13: step-start and
            # step-finish bracket the real content and must be ignored.
            "parts": [
                {"type": "step-start"},
                {"type": "reasoning", "text": "thinking"},
                {"type": "text", "text": "agent says hi"},
                {"type": "tool", "tool": "bash", "state": {"status": "done"}},
                {"type": "text", "text": " and done"},
                {"type": "step-finish"},
            ],
        }

    @up.post("/session/{sid}/abort")
    async def abort(sid: str):
        calls["aborted"] += 1
        return {"ok": True}

    @up.delete("/session/{sid}")
    async def delete_session(sid: str):
        calls["deleted"] += 1
        calls["open_sessions"].discard(sid)
        return {"ok": True}

    return up, calls


def make_llama_upstream(calls: dict) -> FastAPI:
    """llama.cpp: ``/props`` with n_ctx, modalities and template caps
    (trimmed from a live b11445 server running Gemma 4 26B with an mmproj)."""
    up = FastAPI()

    @up.get("/props")
    async def props():
        calls["props"] = calls.get("props", 0) + 1
        return {
            "default_generation_settings": {"n_ctx": 262144, "params": {}},
            "total_slots": 4, "model_alias": "gemma-4-26B-it",
            "modalities": {"vision": True, "video": True, "audio": False},
            "chat_template": "{%- if enable_thinking %}<|think|>{%- endif %}"
                             "{%- if tools %}...{%- endif %}",
            "chat_template_caps": {"supports_tools": True,
                                   "supports_tool_calls": True},
        }

    @up.get("/v1/models")
    async def models():
        return {"data": [{"id": "gemma-4-26B-it", "owned_by": "llamacpp"}]}

    return up


def make_vllm_upstream(calls: dict) -> FastAPI:
    """vLLM: no ``/props``; ``max_model_len`` on each ``/v1/models`` entry."""
    up = FastAPI()

    @up.get("/v1/models")
    async def models():
        calls["vllm_models"] = calls.get("vllm_models", 0) + 1
        return {"object": "list", "data": [
            {"id": "m-a", "object": "model", "max_model_len": 4096},
            {"id": "m-b", "object": "model", "max_model_len": 8192},
        ]}

    return up


def make_hidden_relay_upstream(calls: dict, message: str) -> FastAPI:
    """Another relay in front of vLLM: a relay-shaped catalog with no
    ``max_model_len``, and vLLM's 400 when max_tokens is too large."""
    up = FastAPI()

    @up.get("/v1/models")
    async def models():
        return {"object": "list", "data": [
            {"id": "auto", "object": "model", "owned_by": "relay",
             "relay": {"routing": "active endpoint (hot-swap)"}},
            {"id": "default-model", "object": "model",
             "owned_by": "relay:default-model",
             "relay": {"endpoint": "default-model", "health": "healthy"}},
        ]}

    @up.post("/v1/chat/completions")
    async def chat(request: Request):
        calls["probes"] = calls.get("probes", 0) + 1
        body = await request.json()
        calls["probe_body"] = body
        if body.get("max_tokens", 0) > 131072:
            return JSONResponse(status_code=400, content={"error": {
                "message": message, "type": "BadRequestError",
                "param": "max_tokens", "code": 400}})
        return {"choices": [{"message": {"content": "ok"}}]}

    return up


class _AlwaysFailTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request):
        return httpx.Response(500, content=b"upstream exploded",
                              request=request)


class RoutingTransport(httpx.AsyncBaseTransport):
    def __init__(self, upstream_app: FastAPI,
                 opencode_app: FastAPI | None = None,
                 extra: dict[str, FastAPI] | None = None):
        self._routes: dict[str, httpx.AsyncBaseTransport] = {
            "good": httpx.ASGITransport(app=upstream_app),
            "flaky": _AlwaysFailTransport(),
            "strict": httpx.ASGITransport(app=make_strict_upstream()),
        }
        if opencode_app is not None:
            self._routes["opencode"] = httpx.ASGITransport(app=opencode_app)
        for host, extra_app in (extra or {}).items():
            self._routes[host] = httpx.ASGITransport(app=extra_app)

    async def handle_async_request(self, request):
        transport = self._routes.get(request.url.host)
        if transport is None:
            raise httpx.ConnectError("connection refused", request=request)
        return await transport.handle_async_request(request)
