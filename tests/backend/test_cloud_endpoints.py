"""Cloud endpoints (GAIFA plan llm-servers 1.1): presets, the cloud kind, hidden keys, and the rule that a
provider's API is only reached by name: never `auto`, never a failover target, never GAIFA's GPU slot."""

import pytest

from app.services.gpu_lease import lease_spec
from app.services.router import _infer_kind, alias_only, key_hint


@pytest.fixture
def cloud(client, monkeypatch):
    async def no_probe(eid):           # a create probes at once: never reach a real provider from a test
        return None
    monkeypatch.setattr(client.app.state.prober, "_probe", no_probe)
    return client


def _post(c, **body):
    r = c.post("/admin/endpoints", json=body)
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.parametrize("preset, protocol, url, alias", [
    ("anthropic", "anthropic", "https://api.anthropic.com", "claude"),
    ("openai", "openai", "https://api.openai.com/v1", "openai"),
    ("gemini", "gemini", "https://generativelanguage.googleapis.com", "gemini"),
])
def test_presets(cloud, preset, protocol, url, alias):
    ep = _post(cloud, name=preset.title(), preset=preset, upstream_key="sk-test-0123456789abcd")
    assert (ep["protocol"], ep["base_url"], ep["alias"], ep["kind"], ep["server_type"]) == (protocol, url, alias, "cloud", preset)
    assert ep["has_key"] is True and ep["key_hint"] == "…abcd"
    assert "sk-test" not in str(cloud.get("/admin/endpoints").json())     # the key never comes back
    assert ep["active"] is False                                          # never the default pin


def test_a_preset_keeps_what_was_given(cloud):
    ep = _post(cloud, name="Claude work", preset="anthropic", alias="work", base_url="https://proxy.example/anthropic")
    assert ep["alias"] == "work" and ep["base_url"] == "https://proxy.example/anthropic" and ep["kind"] == "cloud"


def test_no_url_and_no_preset(cloud):
    r = cloud.post("/admin/endpoints", json={"name": "x"})
    assert r.status_code == 422 and "preset" in r.text


def test_kind_inference_and_patch_keeps_cloud(cloud):
    assert _infer_kind("https://api.openai.com/v1", None) == "cloud"
    assert _infer_kind("https://example.com/v1", None, protocol="anthropic") == "cloud"
    assert _infer_kind("http://127.0.0.1:8080/v1", None) == "local"
    assert _infer_kind("https://box.lan/v1", None) == "remote_direct"
    ep = _post(cloud, name="OR", base_url="https://openrouter.example/api/v1", kind="cloud", alias="or")
    r = cloud.patch(f"/admin/endpoints/{ep['id']}", json={"priority": 5})
    assert r.json()["kind"] == "cloud"


def test_cloud_is_never_auto_or_failover(cloud):
    router = cloud.app.state.router
    c = _post(cloud, name="OpenAI", preset="openai", upstream_key="k" * 10)
    assert router.resolve() is None                                   # only a cloud endpoint: no `auto` route
    local = _post(cloud, name="local", base_url="http://127.0.0.1:1/v1")
    assert router.resolve()["id"] == local["id"]
    router.pinned_id = c["id"]                                        # even pinned by hand
    assert router.resolve()["id"] == local["id"]
    assert router.resolve(exclude={local["id"]}) is None              # and never what it fails over to
    assert alias_only(router.endpoints[c["id"]]) and not alias_only(router.endpoints[local["id"]])


def test_cloud_takes_no_gpu_lease():
    assert lease_spec({"protocol": "openai", "kind": "cloud", "base_url": "https://api.openai.com/v1"}) is None
    assert lease_spec({"protocol": "anthropic", "kind": "cloud", "base_url": "https://api.anthropic.com"}) is None
    assert lease_spec({"protocol": "openai", "kind": "local", "base_url": "http://127.0.0.1:7070/v1", "id": "x"})


def test_key_hint():
    assert key_hint(None) is None and key_hint("short") == "…" and key_hint("sk-ant-api03-xyzW") == "…xyzW"
