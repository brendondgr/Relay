"""AIHub plan 31.4: a request to a local engine takes AIHub's GPU slot for its whole reply.

Against a fake AIHub queue (fake_gpu_hub.py) and a fake engine on 127.0.0.1: requests to one engine
share the slot; a request waits while another group (a ComfyUI job) holds it; a stream holds it until
its last chunk; remote endpoints never take it; an upstream error and a client that goes away give it
back; the `gpu_slot` setting turns it off."""

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.requests import Request as StarletteRequest

from app.main import create_app
from app.schemas import EndpointCreate, SettingsPatch
from app.services.gpu_lease import GpuLeases, lease_spec
from fake_gpu_hub import FakeGpuHub


def _request(body: dict, thread: str | None = None, lease: str | None = None) -> StarletteRequest:
    payload = json.dumps(body).encode()
    headers = [(b"content-type", b"application/json")]
    if thread:
        headers.append((b"x-aihub-thread", thread.encode()))
    if lease:
        headers.append((b"x-aihub-gpu-lease", lease.encode()))
    scope = {"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": headers, "query_string": b""}

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    return StarletteRequest(scope, receive)


def make_engine(hub: FakeGpuHub) -> tuple[FastAPI, dict]:
    """A llama-server stand-in: counts requests in flight; a stream notes whether the slot is still held at
    its last chunk."""
    eng = FastAPI()
    st = {"calls": 0, "in_flight": 0, "max_in_flight": 0, "held_at_last_chunk": None, "together": asyncio.Event()}

    @eng.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        st["calls"] += 1
        st["in_flight"] += 1
        st["max_in_flight"] = max(st["max_in_flight"], st["in_flight"])
        if st["in_flight"] >= 2:
            st["together"].set()
        try:
            if body.get("stream"):
                async def gen():
                    for i in range(3):
                        yield f'data: {json.dumps({"choices": [{"index": 0, "delta": {"content": f"t{i}"}}]})}\n\n'
                        await asyncio.sleep(0.01)
                    st["held_at_last_chunk"] = len(hub.held)
                    yield "data: [DONE]\n\n"
                return StreamingResponse(gen(), media_type="text/event-stream")
            if body.get("together"):
                try:
                    await asyncio.wait_for(st["together"].wait(), 2)
                except TimeoutError:
                    pass
            return {"id": "x", "object": "chat.completion", "model": body.get("model"),
                    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "hi"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
        finally:
            st["in_flight"] -= 1

    return eng, st


def broken_engine() -> FastAPI:
    eng = FastAPI()

    @eng.post("/v1/chat/completions")
    async def chat():
        return JSONResponse({"error": {"message": "boom"}}, status_code=500)

    return eng


@pytest.fixture
async def env(cfg, oc_cfg):
    from fake_upstream import RoutingTransport, make_upstream

    hub = FakeGpuHub()
    engine, st = make_engine(hub)
    upstream, _ = make_upstream()
    app = create_app(cfg, oc_cfg)
    async with app.router.lifespan_context(app):
        fake = httpx.AsyncClient(transport=RoutingTransport(
            upstream, extra={"127.0.0.1": engine, "127.0.0.2": broken_engine()}), timeout=5.0)
        app.state.http = fake
        app.state.proxy.http = fake
        app.state.proxy.gpu = GpuLeases(hub_url="http://hub", transport=httpx.ASGITransport(app=hub.app), poll_s=0.1)
        yield app, hub, st
        await app.state.proxy.gpu.close()


def local(app, name="gemma", port=8080, owner=None, key=None) -> dict:
    row = app.state.router.create(EndpointCreate(name=name, base_url=f"http://127.0.0.1:{port}/v1", alias=name))
    if owner:
        app.state.router.set_ownership(row["id"], owner, key, None)
    return row


async def body(resp) -> bytes:
    if hasattr(resp, "body_iterator"):
        out = b""
        async for chunk in resp.body_iterator:
            out += chunk if isinstance(chunk, bytes) else chunk.encode()
        return out
    return resp.body


async def settle(app, hub) -> None:
    """Releases run in the background after a reply ends."""
    for _ in range(100):
        if not hub.held and not app.state.proxy.gpu._held:
            return
        await asyncio.sleep(0.01)


def chat(model: str, **extra) -> dict:
    return {"model": model, "messages": [{"role": "user", "content": "hi"}], **extra}


async def test_two_requests_to_one_engine_run_together(env):
    app, hub, st = env
    local(app, owner="lcpp", key="lcpp:8080")
    proxy = app.state.proxy
    a, b = await asyncio.gather(proxy.handle(_request(chat("gemma", together=True)), "chat/completions"),
                                proxy.handle(_request(chat("gemma", together=True)), "chat/completions"))
    assert a.status_code == b.status_code == 200
    assert st["max_in_flight"] == 2
    seen = [x for x in hub.acquires if x.get("lease")]
    assert {x["group"] for x in seen} == {"lcpp:8080"} and {x["service"] for x in seen} == {"lcpp"}
    assert {x["class"] for x in seen} == {"interactive"} and seen[0]["label"]
    await settle(app, hub)
    assert hub.held == {}


async def test_a_request_waits_while_a_comfy_job_holds_the_slot(env):
    app, hub, st = env
    local(app)
    comfy = hub.hold("comfy")
    task = asyncio.create_task(app.state.proxy.handle(_request(chat("gemma"), thread="t_0123456789ab"), "chat/completions"))
    await asyncio.sleep(0.3)
    assert st["calls"] == 0 and not task.done()
    assert [(w["service"], w["thread"]) for w in hub.waiting] == [("relay", "t_0123456789ab")]
    assert hub.waiting[0]["group"].startswith("relay:")
    await hub.release(comfy)
    resp = await asyncio.wait_for(task, 3)
    assert resp.status_code == 200 and st["calls"] == 1
    await settle(app, hub)
    assert hub.held == {}


async def test_a_stream_holds_the_slot_until_its_last_chunk(env):
    app, hub, st = env
    local(app)
    resp = await app.state.proxy.handle(_request(chat("gemma", stream=True)), "chat/completions")
    assert len(hub.held) == 1                       # headers are back, the reply isn't
    text = await body(resp)
    assert b"[DONE]" in text and st["held_at_last_chunk"] == 1
    await settle(app, hub)
    assert hub.held == {}


async def test_remote_endpoints_never_take_it(env):
    app, hub, _ = env
    app.state.router.create(EndpointCreate(name="remote", base_url="http://good/v1", alias="remote"))
    tunnel = app.state.router.create(EndpointCreate(name="skynet", base_url="http://127.0.0.1:9090/v1", alias="skynet",
                                                    tunnel_command="ssh -N -L 9090:localhost:8000 skynet"))
    assert tunnel["kind"] == "remote_tunnel" and lease_spec(tunnel) is None
    resp = await app.state.proxy.handle(_request(chat("remote")), "chat/completions")
    assert resp.status_code == 200 and hub.acquires == []


def test_lease_spec():
    base = {"id": "e1", "kind": "local", "protocol": "openai", "base_url": "http://127.0.0.1:8080/v1"}
    assert lease_spec(base) == {"service": "relay", "group": "relay:e1"}
    assert lease_spec({**base, "owner": "vllm", "external_key": "vllm:qwen"}) == {"service": "vllm", "group": "vllm:qwen"}
    assert lease_spec({**base, "protocol": "opencode"}) is None               # OpenCode's agent: remote providers
    assert lease_spec({**base, "base_url": "http://10.0.0.5:8080/v1"}) is None
    assert lease_spec({**base, "kind": "remote_direct"}) is None


async def test_an_upstream_error_gives_it_back(env):
    app, hub, _ = env
    app.state.router.create(EndpointCreate(name="broken", base_url="http://127.0.0.2:8081/v1", alias="broken", kind="local"))
    resp = await app.state.proxy.handle(_request(chat("broken")), "chat/completions")
    await body(resp)
    assert resp.status_code >= 500
    assert any(x.get("lease") for x in hub.acquires)
    await settle(app, hub)
    assert hub.held == {} and app.state.proxy.gpu._held == {}


async def test_a_caller_that_goes_away_while_waiting_gives_it_back(env):
    app, hub, st = env
    local(app)
    comfy = hub.hold("comfy")
    task = asyncio.create_task(app.state.proxy.handle(_request(chat("gemma")), "chat/completions"))
    await asyncio.sleep(0.25)
    assert len(hub.waiting) == 1
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    for _ in range(100):
        if not hub.waiting:
            break
        await asyncio.sleep(0.01)
    assert hub.waiting == [] and st["calls"] == 0
    assert app.state.proxy.gate.active == 0
    await hub.release(comfy)


async def test_cancelled_from_the_queue_is_a_503(env):
    app, hub, st = env
    local(app)
    comfy = hub.hold("comfy")
    task = asyncio.create_task(app.state.proxy.handle(_request(chat("gemma")), "chat/completions"))
    await asyncio.sleep(0.25)
    await hub.cancel_waiting()
    resp = await asyncio.wait_for(task, 3)
    assert resp.status_code == 503 and b"GPU" in resp.body and st["calls"] == 0
    await hub.release(comfy)


async def test_a_caller_holding_the_slot_passes_its_lease_down(env):
    app, hub, st = env
    local(app)
    hub.hold("comfy")
    resp = await asyncio.wait_for(app.state.proxy.handle(_request(chat("gemma"), lease="gl_comfyjob"), "chat/completions"), 3)
    assert resp.status_code == 200
    assert [x.get("parent") for x in hub.acquires] == ["gl_comfyjob"]


async def test_the_setting_turns_it_off(env):
    app, hub, st = env
    local(app)
    app.state.settings.update(SettingsPatch(gpu_slot=False))
    hub.hold("comfy")
    resp = await app.state.proxy.handle(_request(chat("gemma")), "chat/completions")
    assert resp.status_code == 200 and hub.acquires == []


async def test_no_hub_no_wait(cfg, oc_cfg, monkeypatch):
    monkeypatch.delenv("AIHUB_URL", raising=False)
    assert GpuLeases().enabled is False
