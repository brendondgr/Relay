"""OpenAI's own API behind relay (GAIFA plan llm-servers 1.6): the cloud rules, and that a local
server's request still goes through byte for byte."""

import json

import pytest

from app.services.adapters.passthrough import cloud_body
from fake_providers import FakeProvider, sse_events

DONE = {"id": "c", "object": "chat.completion", "model": "gpt-5.6",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}}


@pytest.fixture
def fake():
    srv = FakeProvider()
    yield srv
    srv.close()


@pytest.fixture
def relay(client, monkeypatch):
    async def no_probe(eid):
        return None
    monkeypatch.setattr(client.app.state.prober, "_probe", no_probe)
    return client


def chat_calls(fake):
    return [c for c in fake.calls if c.path == "/v1/chat/completions"]


def test_cloud_rules(relay, fake):
    fake.route("POST", "/v1/chat/completions", lambda req: (200, DONE))
    relay.post("/admin/endpoints", json={"name": "OpenAI", "preset": "openai", "base_url": fake.url + "/v1", "upstream_key": "sk-test-1234"})
    relay.post("/v1/chat/completions", json={"model": "openai/gpt-5.6", "max_tokens": 50, "reasoning_effort": "low", "messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "x", "reasoning_details": [{"format": "anthropic"}], "reasoning_content": "t"},
        {"role": "user", "content": "again"}]})
    sent = chat_calls(fake)[0]
    assert sent.json["max_completion_tokens"] == 50 and "max_tokens" not in sent.json and sent.json["reasoning_effort"] == "low"
    assert sent.json["messages"][1] == {"role": "assistant", "content": "x"}
    assert sent.json["model"] == "gpt-5.6" and sent.headers["authorization"] == "Bearer sk-test-1234"


def test_a_stream_always_reports_usage(relay, fake):
    fake.route("POST", "/v1/chat/completions", lambda req: (200, sse_events([(None, {"choices": [{"delta": {"content": "hi"}}]})]) + [b"data: [DONE]\n\n"]))
    relay.post("/admin/endpoints", json={"name": "OpenAI", "preset": "openai", "base_url": fake.url + "/v1", "upstream_key": "k" * 9})
    relay.post("/v1/chat/completions", json={"model": "openai/gpt-5.6", "stream": True, "stream_options": {"include_usage": False},
                                             "messages": [{"role": "user", "content": "hi"}]})
    assert chat_calls(fake)[0].json["stream_options"] == {"include_usage": True}


def test_a_parameter_the_model_rejects_is_dropped(relay, fake):
    def chat(req):
        if "temperature" in req.json:
            return 400, {"error": {"message": "Unsupported value: 'temperature' does not support 0.2 with this model.",
                                   "type": "invalid_request_error", "param": "temperature", "code": "unsupported_value"}}
        return 200, DONE
    fake.route("POST", "/v1/chat/completions", chat)
    relay.post("/admin/endpoints", json={"name": "OpenAI", "preset": "openai", "base_url": fake.url + "/v1", "upstream_key": "k" * 9})
    r = relay.post("/v1/chat/completions", json={"model": "openai/o5", "temperature": 0.2, "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200 and r.headers["x-relay-dropped"] == "temperature"
    assert len(chat_calls(fake)) == 2 and "temperature" not in chat_calls(fake)[1].json


def test_another_400_is_passed_through(relay, fake):
    fake.route("POST", "/v1/chat/completions", lambda req: (400, {"error": {"message": "messages: too long", "type": "invalid_request_error",
                                                                            "param": "messages", "code": "context_length_exceeded"}}))
    relay.post("/admin/endpoints", json={"name": "OpenAI", "preset": "openai", "base_url": fake.url + "/v1", "upstream_key": "k" * 9})
    r = relay.post("/v1/chat/completions", json={"model": "openai/gpt-5.6", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "context_length_exceeded" and len(chat_calls(fake)) == 1


def test_a_local_server_gets_the_body_unchanged(relay, fake):
    fake.route("POST", "/v1/chat/completions", lambda req: (200, DONE))
    relay.post("/admin/endpoints", json={"name": "lcpp", "base_url": fake.url + "/v1", "alias": "box"})
    body = {"model": "box", "max_tokens": 7, "messages": [{"role": "assistant", "content": "x", "reasoning_content": "t"}]}
    relay.post("/v1/chat/completions", json=body)
    sent = chat_calls(fake)[0].json
    assert sent["max_tokens"] == 7 and "max_completion_tokens" not in sent and sent["messages"][0]["reasoning_content"] == "t"


def test_cloud_body_leaves_non_json_alone():
    assert cloud_body(b"not json") == b"not json"
    assert json.loads(cloud_body(b'{"max_completion_tokens": 3, "max_tokens": 9}')) == {"max_completion_tokens": 3, "max_tokens": 9}


# ---- chats only, and the telemetry (GAIFA plan llm-servers 1.7) ------------------------------------------

@pytest.fixture
def chats_only(cfg, oc_cfg, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import create_app
    cfg.cloud_requires_purpose = True
    with TestClient(create_app(cfg, oc_cfg)) as c:
        async def no_probe(eid):
            return None
        monkeypatch.setattr(c.app.state.prober, "_probe", no_probe)
        yield c


def test_cloud_needs_the_chat_purpose(chats_only, fake):
    fake.route("POST", "/v1/chat/completions", lambda req: (200, DONE))
    chats_only.post("/admin/endpoints", json={"name": "OpenAI", "preset": "openai", "base_url": fake.url + "/v1", "upstream_key": "k" * 9})
    chats_only.post("/admin/endpoints", json={"name": "lcpp", "base_url": fake.url + "/v1", "alias": "box"})
    body = {"model": "openai/gpt-5.6", "messages": [{"role": "user", "content": "hi"}]}
    r = chats_only.post("/v1/chat/completions", json=body)
    assert r.status_code == 403 and r.json()["error"]["type"] == "cloud_not_allowed" and not chat_calls(fake)
    r = chats_only.post("/v1/chat/completions", json=body, headers={"X-GAIFA-Purpose": "chat"})
    assert r.status_code == 200 and len(chat_calls(fake)) == 1
    r = chats_only.post("/v1/chat/completions", json={**body, "model": "box"})        # a local server needs nothing
    assert r.status_code == 200


def test_off_by_default(relay, fake):
    fake.route("POST", "/v1/chat/completions", lambda req: (200, DONE))
    relay.post("/admin/endpoints", json={"name": "OpenAI", "preset": "openai", "base_url": fake.url + "/v1", "upstream_key": "k" * 9})
    assert relay.post("/v1/chat/completions", json={"model": "openai/gpt-5.6", "messages": [{"role": "user", "content": "hi"}]}).status_code == 200


def test_requests_carry_the_group_and_cache_tokens(relay, fake):
    done = {**DONE, "usage": {**DONE["usage"], "prompt_tokens_details": {"cached_tokens": 2}}}
    fake.route("POST", "/v1/chat/completions", lambda req: (200, sse_events([(None, {"choices": [{"delta": {"content": "hi"}}]}),
                                                                              (None, {"choices": [], "usage": done["usage"]})]) + [b"data: [DONE]\n\n"]))
    relay.post("/admin/endpoints", json={"name": "OpenAI", "preset": "openai", "base_url": fake.url + "/v1", "upstream_key": "k" * 9})
    relay.post("/admin/endpoints", json={"name": "lcpp", "base_url": fake.url + "/v1", "alias": "box"})
    relay.post("/v1/chat/completions", json={"model": "openai/gpt-5.6", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
    relay.post("/v1/chat/completions", json={"model": "box", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
    import time
    for _ in range(50):                       # the telemetry writer is a background task
        rows = relay.get("/admin/stats/recent").json()["rows"]
        if len([r for r in rows if r.get("grp")]) >= 2:
            break
        time.sleep(0.05)
    cloud = relay.get("/admin/stats/recent", params={"group": "cloud"}).json()["rows"]
    assert [r["grp"] for r in cloud] == ["cloud"] and cloud[0]["cached_tokens"] == 2
    assert [r["grp"] for r in relay.get("/admin/stats/recent", params={"group": "local"}).json()["rows"]] == ["local"]
