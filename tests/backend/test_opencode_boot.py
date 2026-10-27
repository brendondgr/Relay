"""Boot-time registration of the OpenCode endpoint.

This is the whole "clone, run, it works" promise: no setup script, no manual
endpoint creation, no model list to paste in. The cases below are the ways it
can silently not happen.
"""

import pytest

from app.config import OpenCodeConfig
from app.services.opencode_boot import discover_models, ensure_endpoint


def _configure(app, tmp_path, **over):
    """Point the app's OpenCode config at the fake server (routed by hostname
    'opencode' in RoutingTransport)."""
    settings = {
        "enabled": True, "host": "opencode", "port": 80,
        "server_username": "me", "server_password": "s3cret",
        "auth_file": tmp_path / "unused.env",
    }
    app.state.opencode = OpenCodeConfig(**{**settings, **over})
    return app.state.opencode


def test_endpoint_is_created_from_configuration_alone(opencode_env, tmp_path):
    client, app, calls = opencode_env
    assert app.state.router.endpoints == {}  # nothing registered by hand

    cfg = _configure(app, tmp_path)
    eid = ensure_endpoint(app)

    row = app.state.router.endpoints[eid]
    assert row["name"] == cfg.endpoint_name and row["alias"] == "agent"
    assert row["protocol"] == "opencode"
    assert row["base_url"] == "http://opencode:80"
    assert row["upstream_key"] == "me:s3cret"


def test_second_boot_reconciles_instead_of_duplicating(opencode_env, tmp_path):
    client, app, calls = opencode_env
    _configure(app, tmp_path)
    first = ensure_endpoint(app)

    # a restart with a rotated password and a moved port
    _configure(app, tmp_path, port=4097, server_password="rotated")
    second = ensure_endpoint(app)

    assert second == first  # same row: telemetry keeps pointing at it
    assert len(app.state.router.endpoints) == 1
    row = app.state.router.endpoints[first]
    assert row["upstream_key"] == "me:rotated"
    assert row["base_url"] == "http://opencode:4097"


def test_no_credential_means_no_endpoint(opencode_env, tmp_path):
    client, app, calls = opencode_env
    app.state.opencode = OpenCodeConfig(
        enabled=True, host="opencode", port=80, server_password="",
        auth_file=tmp_path / "missing.env")
    assert ensure_endpoint(app) is None
    assert app.state.router.endpoints == {}


def test_disabled_means_no_endpoint(opencode_env, tmp_path):
    client, app, calls = opencode_env
    _configure(app, tmp_path, enabled=False)
    assert ensure_endpoint(app) is None
    assert app.state.router.endpoints == {}


async def test_discovery_publishes_the_free_models(opencode_env, tmp_path):
    """A discovered model that isn't in available_models cannot be named in a
    request — so discovery has to write the list, not just hold it in memory."""
    client, app, calls = opencode_env
    _configure(app, tmp_path)
    eid = ensure_endpoint(app)
    assert app.state.router.available_models(eid) == []

    await discover_models(app, eid)

    published = app.state.router.available_models(eid)
    assert set(published) == {"opencode/hy3-free", "opencode/mimo-v2.5-free"}
    assert app.state.router.state[eid].model in published
    # ...and it survives a restart, because it went to the DB
    row = app.state.db.query_one(
        "SELECT available_models FROM endpoints WHERE id = ?", (eid,))
    assert "hy3-free" in row["available_models"]


async def test_discovered_models_become_addressable(opencode_env, tmp_path):
    client, app, calls = opencode_env
    _configure(app, tmp_path)
    eid = ensure_endpoint(app)
    await discover_models(app, eid)

    ids = {m["id"] for m in client.get("/v1/models").json()["data"]}
    assert {"agent", "agent/opencode/hy3-free", "agent/opencode/mimo-v2.5-free"} <= ids
    assert "opencode/hy3-free" not in ids          # listed once, under the alias; the old id still routes:

    r = client.post("/v1/chat/completions", json={
        "model": "opencode/mimo-v2.5-free",
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text
    assert calls["last_message"]["model"]["modelID"] == "mimo-v2.5-free"


async def test_discovery_gives_up_quietly_when_opencode_is_absent(
        opencode_env, tmp_path, monkeypatch):
    """A dead agent server must not stop relay from serving its other
    endpoints, and must not retry forever."""
    from app.services import opencode_boot

    monkeypatch.setattr(opencode_boot, "_RETRY_DELAYS", (0.0, 0.0))
    client, app, calls = opencode_env
    _configure(app, tmp_path, host="dead-box")
    eid = ensure_endpoint(app)

    await discover_models(app, eid)

    assert app.state.router.available_models(eid) == []
    assert app.state.router.state[eid].health in ("degraded", "failed")


@pytest.mark.parametrize("alias", ["agent", "helper"])
def test_alias_is_configurable(opencode_env, tmp_path, alias):
    client, app, calls = opencode_env
    _configure(app, tmp_path, alias=alias)
    eid = ensure_endpoint(app)
    assert app.state.router.endpoints[eid]["alias"] == alias


def test_proxy_snippets_get_a_model_that_actually_routes(opencode_env, tmp_path):
    """`auto` resolves only to OpenAI-protocol endpoints, so on an agent-only
    relay the dashboard's copy-paste snippets must not offer it."""
    client, app, calls = opencode_env
    _configure(app, tmp_path)
    ensure_endpoint(app)

    assert client.get("/admin/proxy").json()["example_model"] == "agent"

    # ...but an OpenAI endpoint in the pool makes "auto" the right suggestion
    client.post("/admin/endpoints", json={
        "name": "box", "base_url": "http://good/v1", "protocol": "openai",
        "server_type": "llama.cpp"})
    assert client.get("/admin/proxy").json()["example_model"] == "auto"


async def test_models_are_read_live_from_opencode(opencode_env, tmp_path):
    """GAIFA plan llm-servers 6.1: the list comes from OpenCode's own catalog
    each time it's read (free models only, with what OpenCode knows of them),
    not from the allowlist written at boot, and `agent/<provider>/<model>` routes."""
    client, app, calls = opencode_env
    _configure(app, tmp_path)
    eid = ensure_endpoint(app)                     # no discover_models: nothing allowlisted yet
    body = client.get(f"/admin/endpoints/{eid}/models?refresh=1").json()
    assert body["error"] is None
    assert [m["id"] for m in body["models"]] == ["opencode/hy3-free", "opencode/mimo-v2.5-free"]
    hy3 = body["models"][0]
    assert (hy3["display_name"], hy3["context_length"], hy3["max_output"], hy3["tools"], hy3["vision"], hy3["reasoning"]) == \
        ("Hy3 (free)", 128000, 8192, True, True, True)
    mimo = body["models"][1]                       # the server's own shape: capabilities.{toolcall, reasoning, input}
    assert (mimo["context_length"], mimo["tools"], mimo["vision"], mimo["reasoning"]) == (262144, True, False, False)

    data = {m["id"]: m for m in client.get("/v1/models").json()["data"]}
    e = data["agent/opencode/hy3-free"]
    assert e["relay"]["group"] == "agent" and e["relay"]["upstream_model"] == "opencode/hy3-free"
    assert e["meta"]["context_length"] == 128000
    assert not any("claude-opus-5" in k or "gpt-5" in k for k in data)   # paid: never listed

    await app.state.prober.probe_and_report(eid)    # the adapter's free list (its refusal rule)
    r = client.post("/v1/chat/completions", json={
        "model": "agent/opencode/hy3-free", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text
    assert calls["last_message"]["model"] == {"providerID": "opencode", "modelID": "hy3-free"}


async def test_an_unreachable_opencode_keeps_its_last_list(opencode_env, tmp_path, monkeypatch):
    client, app, calls = opencode_env
    _configure(app, tmp_path)
    eid = ensure_endpoint(app)
    assert len(client.get(f"/admin/endpoints/{eid}/models?refresh=1").json()["models"]) == 2
    app.state.router.endpoints[eid]["base_url"] = "http://127.0.0.1:9"   # nothing listens there
    import httpx
    app.state.discovery.http = httpx.AsyncClient(timeout=2.0)
    body = client.get(f"/admin/endpoints/{eid}/models?refresh=1").json()
    assert body["error"] and len(body["models"]) == 2
