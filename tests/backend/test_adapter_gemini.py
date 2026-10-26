"""Gemini behind /v1/chat/completions (GAIFA plan llm-servers 1.5), against a fake of the Gemini API
spoken to by the real google-genai SDK: translation, a function call carrying its thought signature to
the next turn, the JSON Schema as given, a blocked prompt, thinking_level vs budget, usage."""

import json

import pytest

from app.services.adapters.gemini import _merge, translate
from fake_providers import FakeProvider, sse_events

TOOLS = [{"type": "function", "function": {"name": "get_weather", "description": "Weather now",
                                           "parameters": {"type": "object", "properties": {"city": {"type": "string", "pattern": "^[A-Z]"}},
                                                          "required": ["city"], "additionalProperties": False}}}]
STREAM = "/v1beta/models/gemini-3.7-flash:streamGenerateContent"


def turn(call=True, finish="STOP", text="Let me check."):
    ev = [{"candidates": [{"content": {"role": "model", "parts": [{"text": "Weather question.", "thought": True}]}}]},
          {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}}]}]
    last_parts = [{"functionCall": {"name": "get_weather", "args": {"city": "Paris"}, "id": "c1"}, "thoughtSignature": "c2lnMQ=="}] if call else [{"text": "", "thoughtSignature": "c2lnMg=="}]
    ev.append({"candidates": [{"content": {"role": "model", "parts": last_parts}, "finishReason": finish}],
               "usageMetadata": {"promptTokenCount": 20, "candidatesTokenCount": 7, "thoughtsTokenCount": 5,
                                 "cachedContentTokenCount": 8, "totalTokenCount": 32},
               "modelVersion": "gemini-3.7-flash"})
    return sse_events([(None, e) for e in ev])


@pytest.fixture
def fake():
    srv = FakeProvider()
    srv.route("GET", "/v1beta/models", lambda req: (200, {"models": [
        {"name": "models/gemini-3.7-flash", "inputTokenLimit": 1048576, "outputTokenLimit": 65536,
         "supportedGenerationMethods": ["generateContent"], "thinking": True}]}))
    yield srv
    srv.close()


@pytest.fixture
def relay(client, monkeypatch, fake):
    async def no_probe(eid):
        return None
    monkeypatch.setattr(client.app.state.prober, "_probe", no_probe)
    r = client.post("/admin/endpoints", json={"name": "Gemini", "preset": "gemini", "base_url": fake.url, "upstream_key": "g-key-1234"})
    client.get(f"/admin/endpoints/{r.json()['id']}/models")
    return client


def sent(fake, n=0):
    return [c for c in fake.calls if c.path == STREAM][n]


def chunks(text):
    return [json.loads(l[6:]) for l in text.splitlines() if l.startswith("data: ") and l != "data: [DONE]"]


def test_streamed_turn_with_a_function_call(relay, fake):
    fake.route("POST", STREAM, lambda req: (200, turn()))
    r = relay.post("/v1/chat/completions", json={"model": "gemini/gemini-3.7-flash", "stream": True, "temperature": 0.4, "tools": TOOLS,
                                                   "messages": [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Weather in Paris?"}]})
    assert r.status_code == 200
    cs = chunks(r.text)
    deltas = [c["choices"][0]["delta"] for c in cs if c["choices"]]
    assert deltas[0]["reasoning_content"] == "Weather question." and deltas[1]["content"] == "Let me check."
    call = deltas[2]["tool_calls"][0]
    assert call["id"] == "c1" and call["function"]["name"] == "get_weather"
    assert json.loads(deltas[3]["tool_calls"][0]["function"]["arguments"]) == {"city": "Paris"}
    details = next(d["reasoning_details"] for d in deltas if "reasoning_details" in d)[0]
    assert details["format"] == "gemini" and details["parts"][-1]["thought_signature"] == "c2lnMQ=="
    assert [c["choices"][0]["finish_reason"] for c in cs if c["choices"] and c["choices"][0]["finish_reason"]] == ["tool_calls"]
    usage = [c["usage"] for c in cs if c.get("usage")][-1]
    assert usage["prompt_tokens"] == 20 and usage["prompt_tokens_details"]["cached_tokens"] == 8
    assert usage["completion_tokens"] == 12 and usage["completion_tokens_details"] == {"reasoning_tokens": 5}
    req = sent(fake)
    assert req.query.get("alt") == ["sse"] and req.headers.get("x-goog-api-key") == "g-key-1234"
    assert req.json["systemInstruction"]["parts"][0]["text"] == "Be brief."
    assert req.json["generationConfig"]["temperature"] == 0.4
    assert req.json["generationConfig"]["thinkingConfig"] == {"include_thoughts": True}       # the SDK sends it as given
    schema = req.json["tools"][0]["functionDeclarations"][0]["parameters_json_schema"]       # the full JSON Schema, as given
    assert schema["additionalProperties"] is False and schema["properties"]["city"]["pattern"] == "^[A-Z]"
    assert req.json["toolConfig"]["functionCallingConfig"]["mode"] == "AUTO"


def test_the_next_turn_sends_the_models_parts_back(relay, fake):
    fake.route("POST", STREAM, lambda req: (200, turn()))
    first = relay.post("/v1/chat/completions", json={"model": "gemini/gemini-3.7-flash", "tools": TOOLS,
                                                       "messages": [{"role": "user", "content": "Weather in Paris?"}]}).json()
    msg = first["choices"][0]["message"]
    assert msg["tool_calls"][0]["id"] == "c1"
    relay.post("/v1/chat/completions", json={"model": "gemini/gemini-3.7-flash", "tools": TOOLS, "messages": [
        {"role": "user", "content": "Weather in Paris?"},
        {"role": "assistant", "content": msg["content"], "tool_calls": msg["tool_calls"], "reasoning_details": msg["reasoning_details"]},
        {"role": "tool", "tool_call_id": "c1", "content": "{\"temp_c\": 18}"}]})
    contents = sent(fake, 1).json["contents"]
    model_parts = contents[1]["parts"]
    assert contents[1]["role"] == "model" and model_parts[-1]["thoughtSignature"] == "c2lnMQ=="
    assert model_parts[-1]["functionCall"]["name"] == "get_weather"
    assert contents[2] == {"role": "user", "parts": [{"functionResponse": {"name": "get_weather", "response": {"temp_c": 18}, "id": "c1"}}]}


def test_a_plain_request_and_effort(relay, fake):
    fake.route("POST", STREAM, lambda req: (200, turn(call=False, text="Sunny.")))
    body = relay.post("/v1/chat/completions", json={"model": "gemini/gemini-3.7-flash", "reasoning_effort": "xhigh",
                                                      "messages": [{"role": "user", "content": "Hi"}]}).json()
    m = body["choices"][0]
    assert m["message"]["content"] == "Sunny." and m["message"]["reasoning_content"] == "Weather question." and m["finish_reason"] == "stop"
    assert sent(fake).json["generationConfig"]["thinkingConfig"] == {"include_thoughts": True, "thinking_level": "HIGH"}


def test_a_model_that_wants_a_thinking_budget(relay, fake):
    def gen(req):
        if "thinking_level" in req.json["generationConfig"].get("thinkingConfig", {}):
            return 400, {"error": {"code": 400, "message": "thinking_level is not supported for this model; use thinking_budget", "status": "INVALID_ARGUMENT"}}
        return 200, turn(call=False)
    fake.route("POST", STREAM, gen)
    r = relay.post("/v1/chat/completions", json={"model": "gemini/gemini-3.7-flash", "reasoning_effort": "medium", "messages": [{"role": "user", "content": "Hi"}]})
    assert r.status_code == 200
    assert sent(fake, 1).json["generationConfig"]["thinkingConfig"] == {"include_thoughts": True, "thinking_budget": 8192}


def test_a_blocked_prompt_is_a_refusal_not_an_empty_reply(relay, fake):
    fake.route("POST", STREAM, lambda req: (200, sse_events([(None, {"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"},
                                                                    "usageMetadata": {"promptTokenCount": 9, "totalTokenCount": 9}})])))
    body = relay.post("/v1/chat/completions", json={"model": "gemini/gemini-3.7-flash", "messages": [{"role": "user", "content": "x"}]}).json()
    c = body["choices"][0]
    assert c["finish_reason"] == "content_filter" and "PROHIBITED_CONTENT" in c["message"]["refusal"]


def test_a_safety_stop(relay, fake):
    fake.route("POST", STREAM, lambda req: (200, turn(call=False, finish="SAFETY")))
    c = relay.post("/v1/chat/completions", json={"model": "gemini/gemini-3.7-flash", "messages": [{"role": "user", "content": "x"}]}).json()["choices"][0]
    assert c["finish_reason"] == "content_filter" and c["message"]["refusal"] == "Gemini stopped: SAFETY."


def test_errors(relay, fake):
    fake.route("POST", STREAM, lambda req: (403, {"error": {"code": 403, "message": "API key not valid.", "status": "PERMISSION_DENIED"}}))
    r = relay.post("/v1/chat/completions", json={"model": "gemini/gemini-3.7-flash", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 403 and r.json()["error"]["message"] == "API key not valid."
    fake.route("POST", STREAM, lambda req: (503, {"error": {"code": 503, "message": "The model is overloaded.", "status": "UNAVAILABLE"}}))
    r = relay.post("/v1/chat/completions", json={"model": "gemini/gemini-3.7-flash", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 503 and "overloaded" in r.text


def test_translate_tool_choice_media_and_json():
    contents, config = translate({"messages": [{"role": "user", "content": [
        {"type": "text", "text": "see"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAEC"}},
        {"type": "image_url", "image_url": {"url": "https://x/y.webp"}}]}],
        "tools": TOOLS, "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
        "response_format": {"type": "json_schema", "json_schema": {"schema": {"type": "object"}}}, "stop": ["END"], "max_tokens": 100})
    parts = contents[0]["parts"]
    assert parts[1] == {"inline_data": {"mime_type": "image/png", "data": b"\x00\x01\x02"}}
    assert parts[2] == {"file_data": {"file_uri": "https://x/y.webp", "mime_type": "image/webp"}}
    assert config["tool_config"] == {"function_calling_config": {"mode": "ANY", "allowed_function_names": ["get_weather"]}}
    assert config["response_mime_type"] == "application/json" and config["response_json_schema"] == {"type": "object"}
    assert config["stop_sequences"] == ["END"] and config["max_output_tokens"] == 100


def test_merge_joins_text_and_keeps_signed_parts():
    got = _merge([{"text": "a", "thought": True}, {"text": "b", "thought": True}, {"text": "c"}, {"text": "d"},
                  {"text": "", "thought_signature": "x"}, {"function_call": {"name": "f"}}])
    assert got == [{"text": "ab", "thought": True}, {"text": "cd"}, {"text": "", "thought_signature": "x"}, {"function_call": {"name": "f"}}]


def test_probe(relay):
    eid = relay.get("/admin/endpoints").json()[0]["id"]
    r = relay.post(f"/admin/endpoints/{eid}/test").json()
    assert r["ok"] and r["models"] == ["gemini-3.7-flash"]
