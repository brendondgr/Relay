"""Model facts on GET /v1/models: llama.cpp /props, vLLM max_model_len, the
oversized-max_tokens probe behind another relay, the per-endpoint override,
and the cache (services/model_facts.py)."""

import asyncio
import time

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.model_facts import facts_from_props, parse_limit

VLLM_MSG = ("max_tokens=10000000 cannot be greater than"
            " max_model_len=max_total_tokens=131072. Please request fewer"
            " output tokens. (parameter=max_tokens, value=10000000)")
OPENAI_MSG = ("This model's maximum context length is 32768 tokens. However,"
              " you requested 10000009 tokens (9 in the messages, 10000000 in"
              " the completion).")


def _slow_upstream() -> FastAPI:
    up = FastAPI()

    @up.get("/props")
    async def props():
        await asyncio.sleep(30)
        return {}

    return up


@pytest.fixture
def facts_env(cfg, oc_cfg):
    """Yields (client, app, calls) with hosts llama / vllm / hidden /
    hidden2 / slow next to the usual good / flaky / dead."""
    from fake_upstream import (
        RoutingTransport, make_hidden_relay_upstream, make_llama_upstream,
        make_upstream, make_vllm_upstream,
    )

    calls: dict = {}
    hidden2_calls: dict = {}
    upstream, _ = make_upstream()
    extra = {
        "llama": make_llama_upstream(calls),
        "vllm": make_vllm_upstream(calls),
        "hidden": make_hidden_relay_upstream(calls, VLLM_MSG),
        "hidden2": make_hidden_relay_upstream(hidden2_calls, OPENAI_MSG),
        "slow": _slow_upstream(),
    }
    app = create_app(cfg, oc_cfg)
    with TestClient(app) as c:
        fake = httpx.AsyncClient(
            transport=RoutingTransport(upstream, extra=extra), timeout=5.0)
        app.state.http = fake
        app.state.proxy.http = fake
        app.state.prober.http = fake
        app.state.facts.http = fake
        yield c, app, calls


def _register(client, name, url, **extra):
    r = client.post("/admin/endpoints", json={
        "name": name, "base_url": url, "alias": name, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def _meta(client, model_id: str) -> dict:
    r = client.get("/v1/models")
    assert r.status_code == 200
    return next(m["meta"] for m in r.json()["data"] if m["id"] == model_id)


# ---- parsing ---------------------------------------------------------------

def test_parse_limit_reads_both_error_formats():
    assert parse_limit(VLLM_MSG) == 131072
    assert parse_limit("max_tokens too large: max_model_len=4096") == 4096
    assert parse_limit(OPENAI_MSG) == 32768
    assert parse_limit("the request exceeds the available context size"
                       " (8192 tokens)") == 8192
    assert parse_limit("max_tokens=10000000 is too large") is None
    assert parse_limit("") is None


def test_props_without_n_ctx_is_not_llama():
    assert facts_from_props({"title": "some other /props"}) is None
    # Older llama.cpp: no caps, no modalities; the template still says tools.
    old = facts_from_props({"n_ctx": 4096,
                            "chat_template": "{% for tool in tools %}"})
    assert old == {"context_length": 4096, "tools": True, "vision": None,
                   "reasoning": False, "source": "props"}


# ---- the three sources -------------------------------------------------------

def test_llama_props_source(facts_env):
    client, app, calls = facts_env
    _register(client, "gemma", "http://llama/v1")
    meta = _meta(client, "gemma")
    assert meta == {"context_length": 262144, "tools": True, "vision": True,
                    "reasoning": True, "source": "props"}
    # "auto" resolves to the only endpoint, so it carries the same facts.
    assert _meta(client, "auto") == meta


def test_vllm_max_model_len_source(facts_env):
    client, app, calls = facts_env
    _register(client, "big", "http://vllm/v1", model_override="m-b",
              server_type="vLLM")
    assert _meta(client, "big") == {
        "context_length": 8192, "tools": None, "vision": None,
        "reasoning": None, "source": "vllm"}
    assert "probes" not in calls  # max_model_len was enough


def test_probe_behind_another_relay(facts_env):
    client, app, calls = facts_env
    _register(client, "skynet", "http://hidden/v1")
    meta = _meta(client, "skynet")
    assert meta["context_length"] == 131072 and meta["source"] == "probe"
    assert meta["tools"] is None and meta["vision"] is None
    body = calls["probe_body"]
    assert body["max_tokens"] == 10_000_000 and body["stream"] is True
    assert body["model"] == "auto"  # the hidden relay's discovered default
    # One-time: a second listing is served from the cache.
    _meta(client, "skynet")
    assert calls["probes"] == 1


def test_probe_reads_openai_wording(facts_env):
    client, app, _ = facts_env
    _register(client, "other", "http://hidden2/v1")
    assert _meta(client, "other")["context_length"] == 32768


def test_plain_upstream_is_not_probed(facts_env):
    """No props, no max_model_len, not a relay: unknown, and no request with
    an absurd max_tokens sent to a server that might honor it."""
    client, app, _ = facts_env
    _register(client, "good", "http://good/v1")
    assert _meta(client, "good") == {
        "context_length": None, "tools": None, "vision": None,
        "reasoning": None, "source": None}


# ---- override ---------------------------------------------------------------

def test_override_wins_field_by_field(facts_env):
    client, app, _ = facts_env
    ep = _register(client, "gemma", "http://llama/v1", facts_override={
        "context_length": 8192, "tools": False})
    assert ep["facts_override"] == {"context_length": 8192, "tools": False,
                                    "vision": None, "reasoning": None}
    assert _meta(client, "gemma") == {
        "context_length": 8192, "tools": False, "vision": True,
        "reasoning": True, "source": "override"}

    # {} clears it; the detected facts come back without a new read.
    r = client.patch(f"/admin/endpoints/{ep['id']}",
                     json={"facts_override": {}})
    assert r.status_code == 200 and r.json()["facts_override"] is None
    assert _meta(client, "gemma")["context_length"] == 262144

    # A PATCH that doesn't mention it leaves it alone.
    client.patch(f"/admin/endpoints/{ep['id']}",
                 json={"facts_override": {"vision": False}})
    client.patch(f"/admin/endpoints/{ep['id']}", json={"priority": 5})
    meta = _meta(client, "gemma")
    assert meta["vision"] is False and meta["source"] == "props"


def test_override_on_unknown_upstream_names_override(facts_env):
    client, app, _ = facts_env
    _register(client, "good", "http://good/v1",
              facts_override={"tools": True})
    meta = _meta(client, "good")
    assert meta["tools"] is True and meta["source"] == "override"
    assert meta["context_length"] is None


def test_override_rejects_nonsense(facts_env):
    client, app, _ = facts_env
    r = client.post("/admin/endpoints", json={
        "name": "x", "base_url": "http://good/v1",
        "facts_override": {"context_length": 0}})
    assert r.status_code == 422


# ---- failure, cache, protocols ----------------------------------------------

def test_unreachable_upstream_gives_nulls(facts_env):
    client, app, _ = facts_env
    ep = _register(client, "gone", "http://dead/v1")
    assert _meta(client, "gone") == {
        "context_length": None, "tools": None, "vision": None,
        "reasoning": None, "source": None}
    r = client.get(f"/admin/endpoints/{ep['id']}/facts")
    assert r.status_code == 200
    assert r.json()["meta"]["context_length"] is None


def test_slow_upstream_does_not_block_listing(facts_env):
    client, app, _ = facts_env
    app.state.facts.wait = 0.2
    _register(client, "slow", "http://slow/v1")
    t0 = time.monotonic()
    meta = _meta(client, "slow")
    assert time.monotonic() - t0 < 2.0
    assert meta["context_length"] is None


def test_cache_and_refresh_when_model_changes(facts_env):
    client, app, calls = facts_env
    ep = _register(client, "big", "http://vllm/v1", model_override="m-a")
    assert _meta(client, "big")["context_length"] == 4096
    reads = calls["vllm_models"]
    assert _meta(client, "big")["context_length"] == 4096
    assert calls["vllm_models"] == reads  # cached

    client.patch(f"/admin/endpoints/{ep['id']}",
                 json={"model_override": "m-b"})
    assert _meta(client, "big")["context_length"] == 8192
    assert calls["vllm_models"] == reads + 1


def test_facts_route_refresh(facts_env):
    client, app, calls = facts_env
    ep = _register(client, "gemma", "http://llama/v1",
                   facts_override={"context_length": 1000})
    r = client.get(f"/admin/endpoints/{ep['id']}/facts")
    body = r.json()
    assert body["model"] == "gemma-4-26B-it"
    assert body["meta"]["context_length"] == 1000
    assert body["detected"]["context_length"] == 262144
    assert body["checked_ts"] is not None
    before = calls["props"]
    client.get(f"/admin/endpoints/{ep['id']}/facts")
    assert calls["props"] == before
    client.get(f"/admin/endpoints/{ep['id']}/facts?refresh=1")
    assert calls["props"] == before + 1
    assert client.get("/admin/endpoints/nope/facts").status_code == 404


def test_alias_only_protocol_is_never_read(facts_env):
    client, app, calls = facts_env
    _register(client, "agent", "http://llama", protocol="opencode",
              server_type="opencode", facts_override={"tools": True})
    meta = _meta(client, "agent")
    assert meta["context_length"] is None and meta["tools"] is True
    assert "props" not in calls
