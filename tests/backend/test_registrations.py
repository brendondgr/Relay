"""Registrations: idempotent, owner-scoped endpoint upsert/remove by key."""

from fastapi.testclient import TestClient

from app.main import create_app


def _put(client, key, **body):
    body.setdefault("name", key)
    return client.put(f"/admin/registrations/{key}", json=body)


def test_upsert_by_port_is_idempotent(client):
    r = _put(client, "lcpp:7071", name="minicpm-7071", port=1, alias="mini",
             available_models=["minicpm-5-2B", "MiniCPM5-2B"], owner="lcpp",
             meta={"manager_url": "http://127.0.0.1:7700/"})
    assert r.status_code == 200, r.text
    reg = r.json()
    ep = reg["endpoint"]
    assert reg["adopted"] is False and reg["skipped_models"] == []
    assert ep["base_url"] == "http://127.0.0.1:1/v1" and ep["alias"] == "mini"
    assert ep["server_type"] == "llama.cpp" and ep["kind"] == "local"
    assert ep["owner"] == "lcpp" and ep["external_key"] == "lcpp:7071"
    assert ep["meta"] == {"manager_url": "http://127.0.0.1:7700/"}
    assert ep["available_models"] == ["minicpm-5-2B", "MiniCPM5-2B"]

    again = _put(client, "lcpp:7071", name="renamed", port=1, alias="mini", owner="lcpp").json()
    assert again["endpoint"]["id"] == ep["id"]
    assert again["endpoint"]["name"] == "renamed"
    assert len(client.get("/admin/endpoints").json()) == 1


def test_base_url_form_and_validation(client):
    r = _put(client, "ext:a", base_url="http://127.0.0.1:2/v1/")
    assert r.status_code == 200 and r.json()["endpoint"]["base_url"] == "http://127.0.0.1:2/v1"
    assert _put(client, "ext:b").status_code == 422  # neither port nor base_url
    assert _put(client, "ext:c", base_url="ftp://x").status_code == 422
    assert _put(client, "bad key!", port=3).status_code in (404, 422)


def test_adopts_unowned_endpoint_keeping_id_name_and_alias(client):
    legacy = client.post("/admin/endpoints", json={
        "name": "llama.cpp · local", "base_url": "http://127.0.0.1:3/v1", "alias": "local"}).json()
    reg = _put(client, "lcpp:3", name="router-3", port=3, alias="router-3", owner="lcpp").json()
    assert reg["adopted"] is True
    ep = reg["endpoint"]
    assert ep["id"] == legacy["id"]
    assert ep["alias"] == "local" and ep["name"] == "llama.cpp · local"
    assert ep["owner"] == "lcpp"
    # Now keyed: a second upsert finds it by key, not by adoption.
    assert _put(client, "lcpp:3", port=3, owner="lcpp").json()["adopted"] is False
    # An owned endpoint is never adopted by a different key.
    other = _put(client, "other:3", port=3, owner="someone").json()
    assert other["adopted"] is False and other["endpoint"]["id"] != legacy["id"]


def test_models_served_elsewhere_are_skipped_not_fatal(client):
    client.post("/admin/endpoints", json={"name": "a", "base_url": "http://127.0.0.1:4/v1",
                                          "alias": "taken-alias", "available_models": ["m1"]})
    reg = _put(client, "lcpp:5", port=5, available_models=["m1", "m2", "taken-alias"]).json()
    assert reg["skipped_models"] == ["m1", "taken-alias"]
    assert reg["endpoint"]["available_models"] == ["m2"]
    ids = [m["id"] for m in client.get("/v1/models").json()["data"]]
    assert "m2" in ids


def test_alias_conflict_is_409(client):
    client.post("/admin/endpoints", json={"name": "a", "base_url": "http://127.0.0.1:6/v1", "alias": "agent"})
    r = _put(client, "lcpp:7", port=7, alias="agent")
    assert r.status_code == 409 and "already used" in r.json()["detail"]
    assert client.get("/admin/registrations").json() == []


def test_delete_is_idempotent_and_owner_filter(client):
    _put(client, "lcpp:8", port=8, owner="lcpp")
    _put(client, "tool:9", port=9, owner="tool")
    client.post("/admin/endpoints", json={"name": "manual", "base_url": "http://127.0.0.1:10/v1"})
    assert [r["key"] for r in client.get("/admin/registrations?owner=lcpp").json()] == ["lcpp:8"]
    assert {r["key"] for r in client.get("/admin/registrations").json()} == {"lcpp:8", "tool:9"}
    assert client.delete("/admin/registrations/lcpp:8").status_code == 204
    assert client.delete("/admin/registrations/lcpp:8").status_code == 204
    names = {e["name"] for e in client.get("/admin/endpoints").json()}
    assert names == {"tool:9", "manual"}


def test_registrations_survive_restart(cfg, oc_cfg):
    with TestClient(create_app(cfg, oc_cfg)) as c:
        _put(c, "lcpp:11", port=11, owner="lcpp", meta={"k": "v"})
    with TestClient(create_app(cfg, oc_cfg)) as c:
        regs = c.get("/admin/registrations?owner=lcpp").json()
        assert [r["key"] for r in regs] == ["lcpp:11"]
        assert regs[0]["endpoint"]["meta"] == {"k": "v"}
        # still keyed after reload: idempotent upsert, no duplicate
        _put(c, "lcpp:11", port=11, owner="lcpp")
        assert len(c.get("/admin/endpoints").json()) == 1
