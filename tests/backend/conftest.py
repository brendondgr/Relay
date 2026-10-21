"""Shared fixtures: isolated app + DB per test in tmp_path."""

import pytest
from fastapi.testclient import TestClient

from app.config import Config, OpenCodeConfig
from app.db import Database
from app.main import create_app


@pytest.fixture(autouse=True)
def _no_aihub(monkeypatch):
    """No test reaches a real AIHub (its GPU queue, plan 31.4): tests that want one inject a fake."""
    monkeypatch.delenv("AIHUB_URL", raising=False)


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(
        db_path=tmp_path / "test.db",
        log_dir=tmp_path / "logs",
        frontend_dist=tmp_path / "no-dist",
        probe_interval=9999,  # tests drive probes manually
    )


@pytest.fixture
def oc_cfg(tmp_path) -> OpenCodeConfig:
    """OpenCode off by default. Boot registration is real behavior with a
    real network call behind it; a test that wants it opts in (see
    test_opencode_boot.py) rather than every fixture reaching for :4096."""
    return OpenCodeConfig(
        enabled=False, auth_file=tmp_path / "no-auth.env", server_password="")


@pytest.fixture
def db(tmp_path) -> Database:
    d = Database(tmp_path / "unit.db")
    yield d
    d.close()


@pytest.fixture
def app(cfg, oc_cfg):
    return create_app(cfg, oc_cfg)


@pytest.fixture
def client(app):
    with TestClient(app) as c:  # context manager runs lifespan
        yield c


@pytest.fixture
def proxy_env(cfg, oc_cfg):
    """App wired to a fake upstream via a routing transport.

    Yields (client, app, upstream_calls). Hosts: good / flaky / dead.
    """
    import httpx

    from fake_upstream import RoutingTransport, make_upstream

    upstream, calls = make_upstream()
    app = create_app(cfg, oc_cfg)
    with TestClient(app) as c:
        fake = httpx.AsyncClient(
            transport=RoutingTransport(upstream), timeout=5.0)
        app.state.http = fake
        app.state.proxy.http = fake
        app.state.prober.http = fake
        app.state.facts.http = fake
        yield c, app, calls


@pytest.fixture
def opencode_env(cfg, oc_cfg):
    """App wired to a fake ``opencode serve`` on host ``opencode``, alongside
    the usual good/flaky/strict hosts.

    Yields (client, app, opencode_calls).
    """
    import httpx

    from fake_upstream import (
        RoutingTransport, make_opencode_upstream, make_upstream,
    )

    upstream, _ = make_upstream()
    oc_app, oc_calls = make_opencode_upstream()
    app = create_app(cfg, oc_cfg)
    with TestClient(app) as c:
        fake = httpx.AsyncClient(
            transport=RoutingTransport(upstream, oc_app), timeout=5.0)
        app.state.http = fake
        app.state.proxy.http = fake
        app.state.prober.http = fake
        app.state.facts.http = fake
        yield c, app, oc_calls
