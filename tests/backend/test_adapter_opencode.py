"""OpenCode adapter: translation both directions, session hygiene, telemetry.

Everything runs against the fake ``opencode serve`` in ``fake_upstream.py`` —
no real agent server, no provider credentials.
"""

import base64
import json
import time

import pytest

from app.services.adapters.opencode import (
    catalog_models,
    extract_text,
    flatten_messages,
    is_free_model,
    split_model,
)


def _register_opencode(client, name="agent-box", alias="agent",
                       models=None, **extra):
    body = {
        "name": name, "base_url": "http://opencode", "protocol": "opencode",
        "server_type": "opencode", "alias": alias,
        "available_models": models if models is not None else [],
        **extra,
    }
    r = client.post("/admin/endpoints", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _wait_rows(app, n, timeout=3.0) -> list[dict]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = app.state.db.query("SELECT * FROM requests ORDER BY ts")
        if len(rows) >= n:
            return rows
        time.sleep(0.02)
    raise AssertionError(f"telemetry rows never reached {n}")


# ---- pure translation helpers ------------------------------------------
def test_split_model_needs_a_provider_prefix():
    assert split_model("anthropic/claude-sonnet-4-5") == (
        "anthropic", "claude-sonnet-4-5")
    # No prefix -> omit the model and let OpenCode use its configured default.
    assert split_model("claude-sonnet-4-5") is None
    assert split_model(None) is None


def test_flatten_messages_joins_system_and_labels_history():
    prompt, system = flatten_messages([
        {"role": "system", "content": "be terse"},
        {"role": "system", "content": "and kind"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second"},
    ])
    assert system == "be terse\n\nand kind"
    assert prompt == "User: first\n\nAssistant: reply\n\nUser: second"


def test_flatten_messages_single_turn_is_verbatim():
    prompt, system = flatten_messages([{"role": "user", "content": "hi"}])
    assert prompt == "hi" and system is None


def test_flatten_messages_reads_content_blocks():
    prompt, _ = flatten_messages([{"role": "user", "content": [
        {"type": "text", "text": "look "},
        {"type": "image_url", "image_url": {"url": "..."}},
        {"type": "text", "text": "here"},
    ]}])
    assert prompt == "look here"


def test_catalog_models_flattens_providers_default_first():
    payload = {
        "providers": [
            {"id": "opencode", "models": {"hy3-free": {},
                                          "laguna-s-2.1-free": {}}},
            {"id": "openai", "models": [{"id": "gpt-5"}]},
        ],
        "default": {"opencode": "laguna-s-2.1-free"},
    }
    models = catalog_models(payload, free_only=False)
    assert models[0] == "opencode/laguna-s-2.1-free"
    assert set(models) == {"opencode/laguna-s-2.1-free",
                           "opencode/hy3-free", "openai/gpt-5"}


def test_catalog_models_drops_paid_models_by_default():
    models = catalog_models({
        "providers": [{"id": "opencode", "models": {
            "claude-opus-5": {}, "hy3-free": {}, "gpt-5.4-nano": {}}}],
        "default": {"opencode": "claude-opus-5"},
    })
    assert models == ["opencode/hy3-free"]


def test_is_free_model_reads_the_model_half_only():
    assert is_free_model("opencode/nemotron-3-ultra-free")
    assert is_free_model("free-tier-1b")
    assert not is_free_model("opencode/claude-opus-5")
    # a provider called "free" must not launder a paid model
    assert not is_free_model("free/claude-opus-5")
    assert not is_free_model(None)


def test_extract_text_marks_tools_and_drops_reasoning():
    text, tools = extract_text([
        {"type": "reasoning", "text": "hmm"},
        {"type": "text", "text": "a"},
        {"type": "tool", "tool": "bash"},
        {"type": "text", "text": "b"},
    ])
    assert text == "a\n[tool: bash]\nb"
    assert tools == ["bash"]
    assert "hmm" not in text


# ---- probing ------------------------------------------------------------
def test_probe_reports_healthy_and_discovers_catalog(opencode_env):
    client, app, calls = opencode_env
    ep = _register_opencode(client)  # create() probes immediately

    assert calls["health"] >= 1 and calls["providers"] >= 1
    assert app.state.router.state[ep["id"]].health == "healthy"
    # The server's own default (opencode/claude-opus-5) is paid, so it never
    # becomes relay's — the first free model does.
    assert app.state.router.state[ep["id"]].model == "opencode/hy3-free"

    r = client.post(f"/admin/endpoints/{ep['id']}/test")
    assert r.status_code == 200
    result = r.json()
    assert result["ok"] is True
    assert set(result["models"]) == {"opencode/hy3-free",
                                     "opencode/mimo-v2.5-free"}


def test_probe_sends_basic_auth(opencode_env):
    client, app, calls = opencode_env
    _register_opencode(client, upstream_key="me:s3cret")
    expected = base64.b64encode(b"me:s3cret").decode()
    assert calls["auth"] == f"Basic {expected}"


def test_bare_upstream_key_is_the_password_for_the_default_user(opencode_env):
    client, app, calls = opencode_env
    _register_opencode(client, upstream_key="s3cret")
    expected = base64.b64encode(b"opencode:s3cret").decode()
    assert calls["auth"] == f"Basic {expected}"


# ---- non-streaming turns -------------------------------------------------
def test_chat_completion_translated_both_directions(opencode_env):
    client, app, calls = opencode_env
    _register_opencode(client)

    r = client.post("/v1/chat/completions", json={
        "model": "agent",
        "messages": [{"role": "system", "content": "be terse"},
                     {"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text
    body = r.json()

    # Outbound: an OpenCode message body, not an OpenAI one.
    sent = calls["last_message"]
    assert sent["parts"] == [{"type": "text", "text": "hi"}]
    assert sent["system"] == "be terse"
    assert sent["model"] == {"providerID": "opencode",
                             "modelID": "hy3-free"}

    # Inbound: a valid chat.completion, tool part summarized into the text.
    assert body["object"] == "chat.completion"
    choice = body["choices"][0]
    assert choice["message"]["role"] == "assistant"
    assert choice["message"]["content"] == "agent says hi\n[tool: bash]\n and done"
    assert choice["finish_reason"] == "stop"
    assert body["model"] == "opencode/hy3-free"
    # reasoning tokens fold into completion_tokens (4 + 2)
    assert body["usage"] == {"prompt_tokens": 11, "completion_tokens": 6,
                             "total_tokens": 17}


def test_telemetry_takes_opencode_cost_verbatim(opencode_env):
    client, app, calls = opencode_env
    _register_opencode(client)
    client.post("/v1/chat/completions", json={
        "model": "agent", "messages": [{"role": "user", "content": "hi"}]})

    row = _wait_rows(app, 1)[-1]
    assert row["endpoint_name"] == "agent-box"
    assert row["route"] == "chat.completions" and row["ok"] == 1
    assert row["model"] == "opencode/hy3-free"
    assert row["prompt_tokens"] == 11 and row["completion_tokens"] == 6
    assert row["total_tokens"] == 17
    # info.cost wins over relay's price table (which has no entry anyway)
    assert row["cost_usd"] == pytest.approx(0.00123)
    assert row["latency_ms"] > 0 and row["tokens_per_sec"] > 0


def test_session_is_created_and_deleted_per_request(opencode_env):
    client, app, calls = opencode_env
    _register_opencode(client)
    for _ in range(3):
        client.post("/v1/chat/completions", json={
            "model": "agent", "messages": [{"role": "user", "content": "hi"}]})
    assert calls["created"] == 3
    assert calls["deleted"] == 3
    assert calls["open_sessions"] == set()   # nothing leaked
    assert calls["aborted"] == 0             # nothing was cut short


def test_empty_prompt_is_rejected_before_a_session_is_created(opencode_env):
    client, app, calls = opencode_env
    _register_opencode(client)
    r = client.post("/v1/chat/completions", json={
        "model": "agent", "messages": [{"role": "system", "content": "only"}]})
    assert r.status_code == 400
    assert calls["created"] == 0


# ---- streaming -----------------------------------------------------------
def test_streaming_is_synthesized_as_valid_openai_sse(opencode_env):
    client, app, calls = opencode_env
    _register_opencode(client)
    r = client.post("/v1/chat/completions", json={
        "model": "agent", "stream": True,
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")

    frames = [json.loads(line[len("data: "):])
              for line in r.text.splitlines()
              if line.startswith("data: ") and line != "data: [DONE]"]
    assert r.text.rstrip().endswith("data: [DONE]")
    assert frames[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert "agent says hi" in frames[1]["choices"][0]["delta"]["content"]
    assert frames[2]["choices"][0]["finish_reason"] == "stop"
    # trailing usage frame: the inject_stream_usage contract
    assert frames[-1]["usage"]["total_tokens"] == 17
    assert all(f["object"] == "chat.completion.chunk" for f in frames[:-1])

    row = _wait_rows(app, 1)[-1]
    assert row["stream"] == 1 and row["ok"] == 1
    assert row["ttft_ms"] is not None and row["ttft_ms"] > 0
    assert row["completion_tokens"] == 6
    assert calls["deleted"] == 1


# ---- route allowlist -----------------------------------------------------
@pytest.mark.parametrize("route", ["embeddings", "completions"])
def test_unsupported_routes_return_501(opencode_env, route):
    client, app, calls = opencode_env
    _register_opencode(client)
    r = client.post(f"/v1/{route}", json={
        "model": "agent", "input": "hi", "prompt": "hi"})
    assert r.status_code == 501
    assert "opencode" in r.json()["error"]["message"]
    assert calls["created"] == 0

    row = _wait_rows(app, 1)[-1]
    assert row["status"] == 501 and row["ok"] == 0


# ---- model allowlist -----------------------------------------------------
def test_allowlisted_model_routes_verbatim(opencode_env):
    client, app, calls = opencode_env
    _register_opencode(client, models=["opencode/hy3-free",
                                       "opencode/mimo-v2.5-free"])

    r = client.post("/v1/chat/completions", json={
        "model": "opencode/mimo-v2.5-free",
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    # forwarded exactly as asked for, not replaced by the endpoint default
    assert calls["last_message"]["model"] == {"providerID": "opencode",
                                              "modelID": "mimo-v2.5-free"}
    assert r.json()["model"] == "opencode/mimo-v2.5-free"


def test_allowlist_outranks_model_override(opencode_env):
    client, app, calls = opencode_env
    _register_opencode(client, models=["opencode/mimo-v2.5-free"],
                       model_override="opencode/hy3-free")
    client.post("/v1/chat/completions", json={
        "model": "opencode/mimo-v2.5-free",
        "messages": [{"role": "user", "content": "hi"}]})
    assert calls["last_message"]["model"]["modelID"] == "mimo-v2.5-free"

    # ...while the bare alias still takes the override
    client.post("/v1/chat/completions", json={
        "model": "agent", "messages": [{"role": "user", "content": "hi"}]})
    assert calls["last_message"]["model"]["modelID"] == "hy3-free"


def test_allowlist_narrows_the_endpoint_default(opencode_env):
    client, app, calls = opencode_env
    ep = _register_opencode(client, models=["opencode/mimo-v2.5-free"])
    # The probe discovered both free models; only the allowlisted one is
    # usable, so it becomes the endpoint's default too.
    st = app.state.router.state[ep["id"]]
    assert st.models == ["opencode/mimo-v2.5-free"]
    assert st.model == "opencode/mimo-v2.5-free"
    assert "opencode/hy3-free" in st.discovered


def test_models_catalog_lists_the_live_catalog_under_the_alias(opencode_env):
    """The models come from OpenCode's own catalog as `<alias>/<provider>/<model>`
    (GAIFA plan llm-servers 6.1); the allowlisted `<provider>/<model>` ids route
    but aren't listed a second time."""
    client, app, calls = opencode_env
    _register_opencode(client, models=["opencode/hy3-free",
                                       "opencode/mimo-v2.5-free"])
    data = client.get("/v1/models").json()["data"]
    by_id = {m["id"]: m for m in data}
    assert "auto" in by_id and "agent" in by_id
    assert by_id["agent/opencode/hy3-free"]["owned_by"] == "relay:agent-box"
    assert by_id["agent/opencode/hy3-free"]["relay"]["protocol"] == "opencode"
    assert by_id["agent"]["relay"]["endpoint"] == "agent-box"
    assert "opencode/hy3-free" not in by_id


# ---- free models only ----------------------------------------------------
def test_paid_model_in_the_allowlist_is_still_refused(opencode_env):
    """The catalog filter keeps paid models out of discovery, but nothing
    stops an operator typing one into an endpoint's allowlist by hand. The
    turn must be refused rather than billed."""
    client, app, calls = opencode_env
    _register_opencode(client, models=["opencode/claude-opus-5"])

    r = client.post("/v1/chat/completions", json={
        "model": "opencode/claude-opus-5",
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 400
    assert "only free models" in r.json()["error"]["message"]
    assert calls["created"] == 0  # refused before a session was opened

    row = _wait_rows(app, 1)[-1]
    assert row["status"] == 400 and row["ok"] == 0


def test_paid_model_override_cannot_be_reached_through_the_alias(opencode_env):
    """A paid model_override is the other way a bill sneaks in: the alias
    resolves to it before the adapter is called."""
    client, app, calls = opencode_env
    _register_opencode(client, model_override="opencode/claude-opus-5")

    r = client.post("/v1/chat/completions", json={
        "model": "agent", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 400
    assert "only free models" in r.json()["error"]["message"]
    assert calls["created"] == 0


def test_alias_never_lets_opencode_choose_its_own_default(opencode_env):
    """Omitting `model` from the OpenCode message body hands the choice to the
    server, whose default is paid. The alias must resolve to a free model
    explicitly, every time."""
    client, app, calls = opencode_env
    _register_opencode(client)

    r = client.post("/v1/chat/completions", json={
        "model": "agent", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert calls["last_message"]["model"] == {"providerID": "opencode",
                                              "modelID": "hy3-free"}


def test_paid_models_are_absent_from_the_models_catalog(opencode_env):
    client, app, calls = opencode_env
    _register_opencode(client, models=["opencode/hy3-free"])
    ids = {m["id"] for m in client.get("/v1/models").json()["data"]}
    assert "agent/opencode/hy3-free" in ids
    assert not any("opus" in i or "gpt-5" in i for i in ids)


# ---- the permission-hang safeguard ---------------------------------------
def test_hung_turn_times_out_and_aborts_the_session(opencode_env, monkeypatch):
    """A turn parked on an `"ask"` permission has nobody to answer it. The
    deadline bounds it, and the session is aborted rather than left running."""
    from app.services.adapters import opencode as oc

    monkeypatch.setattr(oc, "_TURN_TIMEOUT", 0.3)
    client, app, calls = opencode_env
    _register_opencode(client)
    calls["hang"] = True

    r = client.post("/v1/chat/completions", json={
        "model": "agent", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 504
    assert "permission config" in r.json()["error"]["message"]
    assert calls["aborted"] == 1
    assert calls["open_sessions"] == set()

    row = _wait_rows(app, 1)[-1]
    assert row["status"] == 504 and row["ok"] == 0


# ---- finish reasons ------------------------------------------------------
@pytest.mark.parametrize("finish,expected", [
    ("stop", "stop"),
    ("length", "length"),
    ("tool-calls", "tool_calls"),
    ("content-filter", "content_filter"),
    ("error", "error"),
    ("something-new", "stop"),   # unknown -> lossy but true, never invented
    (None, "stop"),
])
def test_finish_reason_mapping(finish, expected):
    """`info.finish` uses the AI SDK's vocabulary, not OpenAI's."""
    from app.services.adapters.opencode import _finish_reason
    assert _finish_reason(finish, None) == expected


def test_finish_reason_falls_back_to_the_error_when_absent():
    from app.services.adapters.opencode import _finish_reason
    assert _finish_reason(None, "max output tokens reached") == "length"
    assert _finish_reason(None, "provider exploded") == "error"


def test_empty_turn_is_an_error_not_an_empty_success(opencode_env):
    """An unknown modelID gets a 200 with an empty body from the real server —
    identical to what an invented id returns. Reporting that as a successful
    completion would make a typo in the model allowlist look like a model that
    simply had nothing to say."""
    client, app, calls = opencode_env
    _register_opencode(client)
    calls["empty_turn"] = True

    r = client.post("/v1/chat/completions", json={
        "model": "agent", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 502
    msg = r.json()["error"]["message"]
    assert "empty turn" in msg and "/v1/models" in msg
    # the session is still cleaned up on the failure path
    assert calls["deleted"] == 1 and calls["open_sessions"] == set()

    row = _wait_rows(app, 1)[-1]
    assert row["ok"] == 0 and row["status"] == 502
