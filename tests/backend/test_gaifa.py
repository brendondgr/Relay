"""The GAIFA routes: /admin/status and the LLM traffic widget."""

import time

from test_stats import _seed


def test_status_has_the_gaifa_shape(client, app):
    now = time.time()
    _seed(app.state.db, now - 30, tin=100, tout=50, ttft=120)
    _seed(app.state.db, now - 40, tin=10, tout=5, ttft=80)
    s = client.get("/admin/status").json()
    assert s["loaded"] is None and s["busy"] is False
    st = {x["label"]: x for x in s["stats"]}
    assert list(st) == ["Requests today", "Tokens today", "p50 TTFT", "Endpoints"]
    assert st["Requests today"]["value"] == 2 and st["Requests today"]["reset"] == "daily"
    assert st["Tokens today"]["value"] == 165
    assert st["p50 TTFT"]["value"] == 100.0 and st["p50 TTFT"]["unit"] == "ms"
    assert st["Endpoints"]["value"] == "0/0"


def test_status_without_requests_has_no_null_stat(client):
    st = {x["label"]: x for x in client.get("/admin/status").json()["stats"]}
    assert all(x["value"] is not None for x in st.values())
    assert st["p50 TTFT"]["value"] == "–"


def test_traffic_widget_is_widget_data(client, app):
    _seed(app.state.db, time.time() - 60, tin=40, tout=60, ttft=90)
    for rng in ("24h", "7d"):
        w = client.get(f"/admin/hub/widgets/traffic?range={rng}").json()
        assert w["t"] and all(len(s["values"]) == len(w["t"]) for s in w["series"])
        assert [s["name"] for s in w["series"]] == ["Requests", "Tokens", "p50 TTFT (ms)"]
        assert sum(w["series"][0]["values"]) == 1 and sum(w["series"][1]["values"]) == 100
        assert w["t"] == sorted(w["t"])
    assert client.get("/admin/hub/widgets/traffic?range=1h").status_code == 422


def test_the_default_bind_address_is_loopback(monkeypatch):
    monkeypatch.delenv("RELAY_HOST", raising=False)
    from app.config import Config
    assert Config(_env_file=None).host == "127.0.0.1"
