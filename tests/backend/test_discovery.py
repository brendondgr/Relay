"""Live model lists (GAIFA plan llm-servers 1.2): each protocol read from a fake of the provider's own
API through the real SDKs, pagination, the filters, a rejected key, a timeout, a server that isn't there."""

import time

import pytest

from fake_providers import FakeProvider


def claude(mid, effort=("low", "medium", "high", "xhigh", "max"), lifecycle="active", image=True):
    return {"type": "model", "id": mid, "display_name": mid.replace("-", " ").title(), "created_at": "2026-09-01T00:00:00Z",
            "max_input_tokens": 1_000_000, "max_tokens": 128_000, "lifecycle": lifecycle,
            "capabilities": {"thinking": {"supported": True, "types": {"adaptive": {"supported": True}, "enabled": {"supported": False}}},
                             "effort": {"supported": bool(effort), **{lvl: {"supported": lvl in effort} for lvl in ("low", "medium", "high", "xhigh", "max")}},
                             "image_input": {"supported": image}, "pdf_input": {"supported": True}}}


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


def make(relay, **body):
    r = relay.post("/admin/endpoints", json={"upstream_key": "test-key-1234", **body})
    assert r.status_code == 201, r.text
    return r.json()


def test_anthropic_list_with_pages_and_capabilities(relay, fake):
    pages = {None: ([claude("claude-opus-5-5"), claude("claude-haiku-4-5", effort=(), image=False)], True),
             "claude-haiku-4-5": ([claude("claude-old-3", lifecycle="retired"), claude("claude-sonnet-5-5", effort=("low", "medium", "high"))], False)}

    def models(req):
        data, more = pages[(req.query.get("after_id") or [None])[0]]
        return 200, {"data": data, "has_more": more, "first_id": data[0]["id"], "last_id": data[-1]["id"]}
    fake.route("GET", "/v1/models", models)
    ep = make(relay, name="Claude", preset="anthropic", base_url=fake.url)
    body = relay.get(f"/admin/endpoints/{ep['id']}/models").json()
    assert body["error"] is None
    got = {m["id"]: m for m in body["models"]}
    assert list(got) == ["claude-opus-5-5", "claude-haiku-4-5", "claude-sonnet-5-5"]           # the retired one is left out
    opus = got["claude-opus-5-5"]
    assert (opus["context_length"], opus["max_output"], opus["vision"], opus["reasoning"]) == (1_000_000, 128_000, True, True)
    assert opus["effort"] == ["low", "medium", "high", "xhigh", "max"] and opus["thinking_types"] == ["adaptive"]
    assert got["claude-haiku-4-5"]["effort"] is None and got["claude-sonnet-5-5"]["effort"] == ["low", "medium", "high"]
    assert fake.calls[0].headers["authorization"] == "Bearer test-key-1234"     # SDK 2.x sends the key as a Bearer token
    assert fake.calls[0].headers["anthropic-version"] == "2026-10-21"


def test_gemini_list(relay, fake):
    def models(req):
        if not req.query.get("pageToken"):
            return 200, {"models": [
                {"name": "models/gemini-3.7-flash", "displayName": "Gemini 3.7 Flash", "inputTokenLimit": 1048576, "outputTokenLimit": 65536,
                 "supportedGenerationMethods": ["generateContent", "countTokens"], "thinking": True},
                {"name": "models/gemini-embedding-2", "inputTokenLimit": 8192, "supportedGenerationMethods": ["embedContent"]}],
                "nextPageToken": "p2"}
        return 200, {"models": [{"name": "models/gemini-3.5-pro", "inputTokenLimit": 2097152, "outputTokenLimit": 65536,
                                 "supportedGenerationMethods": ["generateContent"], "thinking": True}]}
    fake.route("GET", "/v1beta/models", models)
    ep = make(relay, name="Gemini", preset="gemini", base_url=fake.url)
    body = relay.get(f"/admin/endpoints/{ep['id']}/models").json()
    assert body["error"] is None, body["error"]
    assert [(m["id"], m["context_length"], m["reasoning"]) for m in body["models"]] == [
        ("gemini-3.7-flash", 1048576, True), ("gemini-3.5-pro", 2097152, True)]       # prefix stripped, embeddings out
    assert fake.calls[0].headers.get("x-goog-api-key") == "test-key-1234"


def test_openai_list_and_the_hide_filter(relay, fake):
    ids = ["gpt-5.6", "gpt-5.6-mini", "text-embedding-3-large", "whisper-1", "tts-1", "dall-e-3", "omni-moderation-latest", "o5"]
    fake.route("GET", "/v1/models", lambda req: (200, {"object": "list", "data": [{"id": i, "object": "model"} for i in ids]}))
    ep = make(relay, name="OpenAI", preset="openai", base_url=fake.url + "/v1")
    body = relay.get(f"/admin/endpoints/{ep['id']}/models").json()
    hidden = sorted(m["id"] for m in body["models"] if m["hidden"])
    assert hidden == ["dall-e-3", "omni-moderation-latest", "text-embedding-3-large", "tts-1", "whisper-1"]
    assert fake.calls[0].headers["authorization"] == "Bearer test-key-1234"
    body = relay.patch(f"/admin/endpoints/{ep['id']}/models", json={"hide_filter": False, "hidden": ["o5"]}).json()
    assert [m["id"] for m in body["models"] if m["hidden"]] == ["o5"] and body["hide_filter"] is False
    out = relay.get("/admin/endpoints").json()[0]
    assert out["hidden_models"] == ["o5"] and out["list_models"] is True


def test_a_rejected_key_and_the_last_good_list(relay, fake):
    state = {"ok": True}
    fake.route("GET", "/v1/models", lambda req: (200, {"data": [claude("claude-opus-5-5")], "has_more": False})
               if state["ok"] else (401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}))
    ep = make(relay, name="Claude", preset="anthropic", base_url=fake.url)
    assert relay.get(f"/admin/endpoints/{ep['id']}/models").json()["models"]
    state["ok"] = False
    body = relay.get(f"/admin/endpoints/{ep['id']}/models", params={"refresh": 1}).json()
    assert body["error"].startswith("HTTP 401: key rejected") and "invalid x-api-key" in body["error"]
    assert [m["id"] for m in body["models"]] == ["claude-opus-5-5"]                            # the last good list stays


def test_cached_until_refresh(relay, fake):
    fake.route("GET", "/v1/models", lambda req: (200, {"data": [{"id": "m1"}]}))
    ep = make(relay, name="box", base_url=fake.url + "/v1", list_models=True)
    relay.get(f"/admin/endpoints/{ep['id']}/models")
    relay.get(f"/admin/endpoints/{ep['id']}/models")
    assert len(fake.calls) == 1
    relay.get(f"/admin/endpoints/{ep['id']}/models", params={"refresh": 1})
    assert len(fake.calls) == 2


def test_a_server_that_isnt_running_fails_fast(relay):
    ep = make(relay, name="gone", base_url="http://127.0.0.1:9/v1", list_models=True)
    t0 = time.monotonic()
    body = relay.get(f"/admin/endpoints/{ep['id']}/models").json()
    assert body["models"] == [] and body["error"] and time.monotonic() - t0 < 5


def test_vllm_context_from_its_list(relay, fake):
    fake.route("GET", "/v1/models", lambda req: (200, {"data": [{"id": "qwen3.6-35b-a3b", "max_model_len": 65536}]}))
    ep = make(relay, name="vllm", base_url=fake.url + "/v1", list_models=True)
    assert relay.get(f"/admin/endpoints/{ep['id']}/models").json()["models"][0]["context_length"] == 65536


def test_list_models_defaults():
    from app.services.discovery import listed_models
    assert listed_models({"kind": "cloud"}) and listed_models({"protocol": "gemini", "kind": "remote_direct"})
    assert not listed_models({"kind": "local", "protocol": "openai"})
    assert listed_models({"kind": "local", "list_models": 1}) and not listed_models({"kind": "cloud", "list_models": 0})


def test_unknown_endpoint(relay):
    assert relay.get("/admin/endpoints/nope/models").status_code == 404
