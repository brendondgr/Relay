"""`<server>/<model>` and the grouped catalog (GAIFA plan llm-servers 1.3)."""

import pytest

from fake_providers import FakeProvider
from test_discovery import claude


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
    r = relay.post("/admin/endpoints", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def catalog(relay):
    return {m["id"]: m for m in relay.get("/v1/models").json()["data"]}


def test_listed_models_grouped_with_their_facts(relay, fake):
    fake.route("GET", "/v1/models", lambda req: (200, {"data": [claude("claude-opus-5-5"), claude("claude-haiku-5-5", image=False)], "has_more": False}))
    make(relay, name="Claude", preset="anthropic", base_url=fake.url, upstream_key="k" * 12)
    make(relay, name="lcpp", base_url="http://127.0.0.1:9/v1", alias="gemma")
    cat = catalog(relay)
    opus = cat["claude/claude-opus-5-5"]
    assert opus["relay"]["group"] == "cloud" and opus["relay"]["discovered"] and opus["relay"]["upstream_model"] == "claude-opus-5-5"
    assert opus["relay"]["alias"] == "claude" and opus["relay"]["protocol"] == "anthropic"
    assert opus["meta"]["context_length"] == 1_000_000 and opus["meta"]["vision"] is True and opus["meta"]["source"] == "server"
    assert cat["claude/claude-haiku-5-5"]["meta"]["vision"] is False
    assert cat["gemma"]["relay"]["group"] == "local" and not any(k.startswith("gemma/") for k in cat)   # a local server isn't listed by default
    assert cat["claude"]["relay"]["group"] == "cloud"


def test_hidden_models_leave_the_catalog(relay, fake):
    fake.route("GET", "/v1/models", lambda req: (200, {"data": [claude("claude-opus-5-5"), claude("claude-haiku-5-5")], "has_more": False}))
    ep = make(relay, name="Claude", preset="anthropic", base_url=fake.url, upstream_key="k" * 12)
    relay.patch(f"/admin/endpoints/{ep['id']}/models", json={"hidden": ["claude-haiku-5-5"]})
    assert "claude/claude-haiku-5-5" not in catalog(relay) and "claude/claude-opus-5-5" in catalog(relay)


def test_override_wins_over_the_provider(relay, fake):
    fake.route("GET", "/v1/models", lambda req: (200, {"data": [claude("claude-opus-5-5")], "has_more": False}))
    make(relay, name="Claude", preset="anthropic", base_url=fake.url, upstream_key="k" * 12, facts_override={"context_length": 200000})
    assert catalog(relay)["claude/claude-opus-5-5"]["meta"]["context_length"] == 200000


def test_routing_forms(relay):
    router = relay.app.state.router
    box = make(relay, name="box", base_url="http://127.0.0.1:9/v1", alias="box", available_models=["qwen3-8b"])
    assert router.resolve_request_model("box") == (router.endpoints[box["id"]], None)              # the alias
    assert router.resolve_request_model("box/m2") == (router.endpoints[box["id"]], "m2")           # <alias>/<model>
    assert router.resolve_request_model("box/org/model") == (router.endpoints[box["id"]], "org/model")
    assert router.resolve_request_model("qwen3-8b") == (router.endpoints[box["id"]], "qwen3-8b")   # an allowlist id
    assert router.resolve_request_model("nobox/m2") == (None, None)


def test_an_alias_shadows_another_endpoints_allowlist(relay):
    router = relay.app.state.router
    oai = make(relay, name="OpenAI", base_url="http://127.0.0.1:9/v1", alias="openai")
    make(relay, name="agent", base_url="http://127.0.0.1:8/v1", alias="agent", available_models=["openai/gpt-x", "anthropic/claude-y"])
    cat = catalog(relay)
    assert "openai/gpt-x" not in cat and "anthropic/claude-y" in cat
    assert router.resolve_request_model("openai/gpt-x") == (router.endpoints[oai["id"]], "gpt-x")


def test_an_alias_cant_contain_a_slash(relay):
    r = relay.post("/admin/endpoints", json={"name": "x", "base_url": "http://127.0.0.1:9/v1", "alias": "a/b"})
    assert r.status_code == 422


def test_a_request_to_alias_slash_model_goes_upstream_as_that_model(relay, fake):
    fake.route("POST", "/v1/chat/completions", lambda req: (200, {"id": "x", "object": "chat.completion", "model": req.json["model"],
                                                                  "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
                                                                  "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}}))
    make(relay, name="box", base_url=fake.url + "/v1", alias="box")
    r = relay.post("/v1/chat/completions", json={"model": "box/m2", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text
    assert fake.calls[-1].json["model"] == "m2"


def test_an_alias_that_is_also_its_model_id_is_listed_once(relay):
    """lcpp names an endpoint after its model (alias and allowlist both `smollm2`): one entry, not two."""
    make(relay, name="smollm2-7071", base_url="http://127.0.0.1:9/v1", alias="smollm2", available_models=["smollm2", "smollm2-q8"])
    ids = [m["id"] for m in relay.get("/v1/models").json()["data"]]
    assert ids.count("smollm2") == 1 and ids.count("smollm2-q8") == 1
