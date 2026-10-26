"""Claude behind /v1/chat/completions (GAIFA plan llm-servers 1.4), against a fake Messages API spoken
to by the real anthropic SDK: the request translation, thinking and its signature carried to the next
turn, streamed tool calls, usage with cache tokens, the refused-feature retries and the errors."""

import json

import pytest

from app.services.adapters.anthropic import _snap_effort, translate
from fake_providers import FakeProvider, sse_events
from test_discovery import claude

TOOLS = [{"type": "function", "function": {"name": "get_weather", "description": "Weather now",
                                           "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]


def turn_events(stop="tool_use", text="Checking.", sig="sig-abc", tool=True, refusal=None):
    ev = [("message_start", {"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [], "stop_reason": None,
        "stop_sequence": None, "usage": {"input_tokens": 12, "output_tokens": 1, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20}}}),
          ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}}),
          ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "The user wants weather."}}),
          ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": sig}}),
          ("content_block_stop", {"type": "content_block_stop", "index": 0}),
          ("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}}),
          ("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": text}}),
          ("content_block_stop", {"type": "content_block_stop", "index": 1})]
    if tool:
        ev += [("content_block_start", {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {}}}),
               ("content_block_delta", {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": "{\"city\": "}}),
               ("content_block_delta", {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": "\"Paris\"}"}}),
               ("content_block_stop", {"type": "content_block_stop", "index": 2})]
    delta = {"stop_reason": stop, "stop_sequence": None}
    if refusal:
        delta["stop_details"] = {"type": "refusal", "category": "cyber", "explanation": refusal}
    ev += [("message_delta", {"type": "message_delta", "delta": delta, "usage": {"output_tokens": 42}}),
           ("message_stop", {"type": "message_stop"})]
    return sse_events(ev)


@pytest.fixture
def fake():
    srv = FakeProvider()
    srv.route("GET", "/v1/models", lambda req: (200, {"data": [claude("claude-opus-5-5"), claude("claude-sonnet-5-5", effort=("low", "medium", "high"))], "has_more": False}))
    yield srv
    srv.close()


@pytest.fixture
def relay(client, monkeypatch, fake):
    async def no_probe(eid):
        return None
    monkeypatch.setattr(client.app.state.prober, "_probe", no_probe)
    r = client.post("/admin/endpoints", json={"name": "Claude", "preset": "anthropic", "base_url": fake.url, "upstream_key": "sk-ant-test-1234"})
    assert r.status_code == 201
    client.get(f"/admin/endpoints/{r.json()['id']}/models")          # the listing the adapter reads (output cap, effort, thinking)
    return client


def messages_calls(fake):
    return [c for c in fake.calls if c.path == "/v1/messages"]


def sse_chunks(text):
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ") and line != "data: [DONE]"]


def test_streamed_turn_with_thinking_and_a_tool_call(relay, fake):
    fake.route("POST", "/v1/messages", lambda req: (200, turn_events()))
    r = relay.post("/v1/chat/completions", json={
        "model": "claude/claude-opus-5-5", "stream": True, "temperature": 0.3, "tools": TOOLS,
        "messages": [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Weather in Paris?"}]})
    assert r.status_code == 200 and r.headers["x-relay-dropped"] == "temperature"
    chunks = sse_chunks(r.text)
    assert r.text.rstrip().endswith("data: [DONE]")
    deltas = [c["choices"][0]["delta"] for c in chunks if c["choices"]]
    assert deltas[0]["role"] == "assistant" and deltas[0]["reasoning_content"] == "The user wants weather."
    assert {"content": "Checking."} in deltas
    assert deltas[2]["tool_calls"][0] == {"index": 0, "id": "toolu_1", "type": "function", "function": {"name": "get_weather", "arguments": ""}}
    assert "".join(d["tool_calls"][0]["function"]["arguments"] for d in deltas[3:5]) == '{"city": "Paris"}'
    details = next(d["reasoning_details"] for d in deltas if "reasoning_details" in d)
    assert details == [{"format": "anthropic", "model": "claude-opus-5-5", "block": {"type": "thinking", "thinking": "The user wants weather.", "signature": "sig-abc"}}]
    assert [c["choices"][0]["finish_reason"] for c in chunks if c["choices"] and c["choices"][0]["finish_reason"]] == ["tool_calls"]
    usage = [c["usage"] for c in chunks if c.get("usage")][-1]
    assert usage["prompt_tokens"] == 132 and usage["prompt_tokens_details"] == {"cached_tokens": 100, "cache_creation_tokens": 20}
    assert usage["completion_tokens"] == 42
    sent = messages_calls(fake)[0]
    assert sent.json["system"] == "Be brief." and sent.json["messages"] == [{"role": "user", "content": [{"type": "text", "text": "Weather in Paris?"}]}]
    assert sent.json["max_tokens"] == 128_000                                        # Claude requires it: the model's own cap
    assert sent.json["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert sent.json["cache_control"] == {"type": "ephemeral"} and sent.json["fallbacks"] == "default"
    assert "temperature" not in sent.json and sent.headers["anthropic-beta"] == "server-side-fallback-2026-07-01"
    assert sent.json["tools"][0]["input_schema"]["required"] == ["city"] and sent.json["tool_choice"] == {"type": "auto"}
    assert sent.json["stream"] is True and sent.json["model"] == "claude-opus-5-5"


def test_the_second_turn_carries_the_signature_back(relay, fake):
    fake.route("POST", "/v1/messages", lambda req: (200, turn_events(stop="end_turn", tool=False, text="Sunny.")))
    first = relay.post("/v1/chat/completions", json={"model": "claude/claude-opus-5-5", "tools": TOOLS,
                                                       "messages": [{"role": "user", "content": "Weather in Paris?"}]}).json()
    msg = first["choices"][0]["message"]
    assistant = {"role": "assistant", "content": msg["content"], "reasoning_details": msg["reasoning_details"],
                 "tool_calls": [{"id": "toolu_1", "type": "function", "function": {"name": "get_weather", "arguments": "{\"city\": \"Paris\"}"}}]}
    relay.post("/v1/chat/completions", json={"model": "claude/claude-opus-5-5", "tools": TOOLS, "messages": [
        {"role": "user", "content": "Weather in Paris?"}, assistant,
        {"role": "tool", "tool_call_id": "toolu_1", "content": "18 °C, sunny"}]})
    sent = messages_calls(fake)[1].json["messages"]
    assert sent[1]["role"] == "assistant"
    assert sent[1]["content"][0] == {"type": "thinking", "thinking": "The user wants weather.", "signature": "sig-abc"}   # first, unchanged
    assert sent[1]["content"][-1] == {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "Paris"}}
    assert sent[2] == {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "18 °C, sunny"}]}


def test_a_plain_request_is_assembled(relay, fake):
    fake.route("POST", "/v1/messages", lambda req: (200, turn_events()))
    body = relay.post("/v1/chat/completions", json={"model": "claude/claude-opus-5-5", "tools": TOOLS,
                                                      "messages": [{"role": "user", "content": "Weather?"}]}).json()
    m = body["choices"][0]["message"]
    assert m["content"] == "Checking." and m["reasoning_content"] == "The user wants weather."
    assert m["tool_calls"] == [{"id": "toolu_1", "type": "function", "function": {"name": "get_weather", "arguments": "{\"city\": \"Paris\"}"}}]
    assert body["choices"][0]["finish_reason"] == "tool_calls" and body["usage"]["total_tokens"] == 174
    assert body["model"] == "claude-opus-5-5"


def test_a_refused_forced_tool_choice_is_retried_as_auto(relay, fake):
    def messages(req):
        if req.json.get("tool_choice", {}).get("type") == "any":
            return 400, {"type": "error", "error": {"type": "invalid_request_error", "message": "tool_choice: type \"tool\" and \"any\" are not supported for this model."}}
        return 200, turn_events()
    fake.route("POST", "/v1/messages", messages)
    r = relay.post("/v1/chat/completions", json={"model": "claude/claude-opus-5-5", "tools": TOOLS, "tool_choice": "required",
                                                   "messages": [{"role": "user", "content": "Weather?"}]})
    assert r.status_code == 200 and r.headers["x-relay-dropped"] == "tool_choice"
    assert [c.json["tool_choice"]["type"] for c in messages_calls(fake)] == ["any", "auto"]


def test_a_refused_fallback_is_retried_without_it(relay, fake):
    def messages(req):
        if "fallbacks" in req.json:
            return 400, {"type": "error", "error": {"type": "invalid_request_error", "message": "fallbacks: not supported for this model"}}
        return 200, turn_events(tool=False, stop="end_turn")
    fake.route("POST", "/v1/messages", messages)
    r = relay.post("/v1/chat/completions", json={"model": "claude/claude-opus-5-5", "messages": [{"role": "user", "content": "Hi"}]})
    assert r.status_code == 200 and "x-relay-dropped" not in r.headers
    assert "anthropic-beta" not in messages_calls(fake)[1].headers


def test_effort_snaps_to_the_models_ladder(relay, fake):
    fake.route("POST", "/v1/messages", lambda req: (200, turn_events(tool=False, stop="end_turn")))
    relay.post("/v1/chat/completions", json={"model": "claude/claude-sonnet-5-5", "reasoning_effort": "max",
                                             "messages": [{"role": "user", "content": "Hi"}]})
    assert messages_calls(fake)[0].json["output_config"] == {"effort": "high"}       # sonnet's ladder stops at high
    assert _snap_effort("minimal", None) == "low" and _snap_effort("xhigh", ["low", "medium", "high"]) == "high"


def test_errors(relay, fake):
    fake.route("POST", "/v1/messages", lambda req: (401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}))
    r = relay.post("/v1/chat/completions", json={"model": "claude/claude-opus-5-5", "messages": [{"role": "user", "content": "Hi"}]})
    assert r.status_code == 401 and r.json()["error"]["message"] == "invalid x-api-key" and r.json()["error"]["type"] == "authentication_error"
    fake.route("POST", "/v1/messages", lambda req: (529, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}))
    r = relay.post("/v1/chat/completions", json={"model": "claude/claude-opus-5-5", "messages": [{"role": "user", "content": "Hi"}]})
    assert r.status_code == 529 and "Overloaded" in r.text


def test_an_error_mid_stream(relay, fake):
    events = turn_events(tool=False)[:7] + sse_events([("error", {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})])
    fake.route("POST", "/v1/messages", lambda req: (200, events))
    r = relay.post("/v1/chat/completions", json={"model": "claude/claude-opus-5-5", "stream": True, "messages": [{"role": "user", "content": "Hi"}]})
    chunks = sse_chunks(r.text)
    assert any(c["choices"] and c["choices"][0]["delta"].get("content") == "Checking." for c in chunks if "choices" in c)
    assert chunks[-1]["error"]["message"] == "Overloaded" and r.text.rstrip().endswith("data: [DONE]")


def test_a_refusal(relay, fake):
    fake.route("POST", "/v1/messages", lambda req: (200, turn_events(stop="refusal", tool=False, refusal="Not something I can help with.")))
    body = relay.post("/v1/chat/completions", json={"model": "claude/claude-opus-5-5", "messages": [{"role": "user", "content": "x"}]}).json()
    assert body["choices"][0]["finish_reason"] == "content_filter" and body["choices"][0]["message"]["refusal"] == "Not something I can help with."


def test_translate_media_and_system_messages():
    p = translate({"messages": [
        {"role": "system", "content": "S1"},
        {"role": "user", "content": [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                                     {"type": "image_url", "image_url": {"url": "https://x/y.jpg"}},
                                     {"type": "file", "file": {"filename": "a.pdf", "file_data": "data:application/pdf;base64,JVBE"}}]},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "next"},
        {"role": "system", "content": "Terse now."}], "stop": "END",
        "response_format": {"type": "json_schema", "json_schema": {"name": "x", "schema": {"type": "object"}}}})
    assert p["system"] == "S1"
    blocks = p["messages"][0]["content"]
    assert blocks[1] == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
    assert blocks[2] == {"type": "image", "source": {"type": "url", "url": "https://x/y.jpg"}}
    assert blocks[3] == {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBE"}, "title": "a.pdf"}
    assert p["messages"][-1] == {"role": "system", "content": "Terse now."}           # a later one stays mid-conversation
    assert p["stop_sequences"] == ["END"] and p["output_config"] == {"format": {"type": "json_schema", "schema": {"type": "object"}}}


def test_a_refused_mid_conversation_system_message_is_lifted(relay, fake):
    def messages(req):
        if any(m["role"] == "system" for m in req.json["messages"]):
            return 400, {"type": "error", "error": {"type": "invalid_request_error", "message": "role 'system' is not supported on this model"}}
        return 200, turn_events(tool=False, stop="end_turn")
    fake.route("POST", "/v1/messages", messages)
    relay.post("/v1/chat/completions", json={"model": "claude/claude-opus-5-5", "messages": [
        {"role": "system", "content": "S"}, {"role": "user", "content": "hi"}, {"role": "system", "content": "Terse."}]})
    assert messages_calls(fake)[1].json["system"] == "S\n\nTerse."


def test_probe_lists_models(relay):
    eid = relay.get("/admin/endpoints").json()[0]["id"]
    r = relay.post(f"/admin/endpoints/{eid}/test").json()
    assert r["ok"] and "claude-opus-5-5" in r["models"]
