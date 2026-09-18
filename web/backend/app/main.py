"""FastAPI application factory and lifespan wiring.

Boot order: logging -> DB -> settings -> telemetry writer + live tracker ->
(router registry, health prober, tunnel supervisor attach in later modules)
-> the OpenCode endpoint reconcile -> background housekeeping (live sampler,
retention pruning). In production the built dashboard (web/frontend/dist) is
served statically from "/".
"""

import asyncio
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response

from app import __version__
from app.config import Config, OpenCodeConfig, config, opencode_config
from app.core.logging import get_logger, setup_logging
from app.db import Database
from app.routes import (
    admin_endpoints,
    admin_registrations,
    admin_settings,
    admin_stats,
    admin_tunnels,
    v1,
)
from app.services.health import HealthProber
from app.services.opencode_boot import discover_models, ensure_endpoint
from app.services.proxy import ProxyService
from app.services.router import Router
from app.services.settings_store import SettingsStore
from app.services.stats import StatsService
from app.services.telemetry import LiveTracker, TelemetryWriter
from app.services.tunnel_sessions import TunnelSessionManager
from app.services.tunnels import TunnelManager

log = get_logger("app")

_SAMPLE_INTERVAL = 1.5
_PRUNE_INTERVAL = 3600.0


async def _housekeeping(app: FastAPI) -> None:
    last_prune = 0.0
    while True:
        await asyncio.sleep(_SAMPLE_INTERVAL)
        app.state.live.sample()
        now = asyncio.get_event_loop().time()
        if now - last_prune > _PRUNE_INTERVAL:
            last_prune = now
            retention = app.state.settings.current.retention_days
            try:
                await app.state.telemetry.prune(retention)
            except Exception:
                log.exception("retention prune failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = app.state.cfg
    # The pool has to be at least as wide as the admission gate, plus room for
    # the extra calls a single logical request makes (OpenCode does session
    # create -> message -> delete). A pool narrower than the gate turns relay's
    # own saturation into PoolTimeouts that the health state machine reads as
    # a broken endpoint — see UpstreamSaturated.
    pool_max = cfg.pool_connections or (cfg.max_concurrency * 2 + 32)
    keepalive = cfg.pool_keepalive or max(32, cfg.max_concurrency // 2)
    app.state.http = httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=cfg.connect_timeout, read=cfg.read_timeout,
            write=cfg.write_timeout, pool=cfg.pool_timeout),
        limits=httpx.Limits(max_connections=pool_max,
                            max_keepalive_connections=keepalive),
    )
    # Probes get their own small pool. Sharing the proxy's is how a burst of
    # traffic used to fail the health check for the very endpoint serving it:
    # probes queued behind proxied requests, timed out, and three of those in
    # a row dropped a healthy endpoint out of rotation. Health has to be
    # measurable precisely when relay is busiest.
    app.state.probe_http = httpx.AsyncClient(
        timeout=httpx.Timeout(cfg.probe_timeout, pool=cfg.probe_timeout),
        limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
    )
    app.state.prober = HealthProber(
        app.state.router, app.state.probe_http,
        interval=cfg.probe_interval, timeout=cfg.probe_timeout)
    app.state.proxy = ProxyService(
        app.state.http, app.state.router, app.state.telemetry,
        app.state.live, app.state.settings,
        max_concurrency=cfg.max_concurrency,
        queue_limit=cfg.queue_limit, queue_timeout=cfg.queue_timeout,
        max_body_bytes=cfg.max_body_bytes)
    app.state.tunnels = TunnelManager(app.state.db, app.state.http)
    app.state.tunnel_sessions = TunnelSessionManager()
    await app.state.telemetry.start()
    await app.state.prober.start()
    # Do NOT auto-connect tunnels on boot: interactive tunnels are connected
    # manually from the Endpoints UI, and legacy tunnels are opt-in.
    await app.state.tunnels.start_supervisor(autostart=False)
    # Registered here rather than by a setup script: every way of starting
    # relay should end up with the same agent endpoint (see opencode_boot).
    agent_id = ensure_endpoint(app)
    discovery = (asyncio.create_task(discover_models(app, agent_id),
                                     name="opencode-discovery")
                 if agent_id else None)
    task = asyncio.create_task(_housekeeping(app), name="housekeeping")
    log.info("relay started", extra={"data": {
        "version": __version__, "port": app.state.cfg.port,
        "db": str(app.state.cfg.db_path),
        "max_concurrency": cfg.max_concurrency,
        "queue_limit": cfg.queue_limit,
        "pool_connections": pool_max}})
    # relay has no auth layer, and an agent endpoint runs shell commands and
    # edits files on its host by design. On a non-loopback bind that makes the
    # port a remote shell for anyone who can reach it. Loud, once, at boot —
    # quietly proxying it would be worse.
    agents = [r["name"] for r in app.state.router.endpoints.values()
              if (r.get("protocol") or "openai") != "openai"]
    if agents and cfg.host not in ("127.0.0.1", "::1", "localhost"):
        log.warning(
            "agent endpoints are reachable, unauthenticated, from the network:"
            " anyone who can reach this port can run shell commands on the"
            " OpenCode host. Set RELAY_HOST=127.0.0.1 to keep it local.",
            extra={"data": {"endpoints": agents, "host": cfg.host}})
    try:
        yield
    finally:
        task.cancel()
        if discovery:
            discovery.cancel()
        await app.state.tunnel_sessions.shutdown()
        await app.state.tunnels.stop_supervisor()
        await app.state.prober.stop()
        await app.state.telemetry.stop()
        await app.state.http.aclose()
        await app.state.probe_http.aclose()
        app.state.db.close()
        log.info("relay stopped")


def create_app(cfg: Config | None = None,
               oc_cfg: OpenCodeConfig | None = None) -> FastAPI:
    cfg = cfg or config
    setup_logging(cfg.log_dir, cfg.log_level)

    app = FastAPI(title="relay", version=__version__, lifespan=lifespan)
    app.state.cfg = cfg
    app.state.opencode = oc_cfg or opencode_config
    app.state.db = Database(cfg.db_path, threads=cfg.db_threads)
    app.state.settings = SettingsStore(app.state.db, boot_port=cfg.port)
    app.state.telemetry = TelemetryWriter(app.state.db)
    app.state.live = LiveTracker()
    app.state.router = Router(
        app.state.db, unhealthy_after=cfg.unhealthy_after,
        recover_after=cfg.recover_after)
    app.state.stats = StatsService(app.state.db, app.state.live)

    @app.middleware("http")
    async def cors_and_access_log(request: Request, call_next):
        # Dynamic CORS honoring the live "allow_cors" setting; admin/stats
        # traffic is same-origin so this mainly serves /v1 browser clients.
        if request.method == "OPTIONS" and app.state.settings.current.allow_cors:
            return Response(status_code=204, headers=_cors_headers())
        response = await call_next(request)
        if app.state.settings.current.allow_cors:
            for k, v in _cors_headers().items():
                response.headers.setdefault(k, v)
        return response

    def _cors_headers() -> dict:
        return {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, PATCH, PUT, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Admin-Token",
        }

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "uptime_s": round(app.state.settings.uptime_s(), 1),
        }

    app.include_router(v1.router)
    app.include_router(admin_endpoints.router)
    app.include_router(admin_settings.router)
    app.include_router(admin_stats.router)
    app.include_router(admin_tunnels.router)
    app.include_router(admin_registrations.router)

    _mount_static_dashboard(app, cfg)
    return app


def _mount_static_dashboard(app: FastAPI, cfg: Config) -> None:
    """Serve the built Astro dashboard from / when dist exists."""
    if not cfg.frontend_dist.is_dir():
        log.info("frontend dist not found; API-only mode",
                 extra={"data": {"path": str(cfg.frontend_dist)}})
        return
    from fastapi.staticfiles import StaticFiles

    app.mount(
        "/", StaticFiles(directory=cfg.frontend_dist, html=True), name="dashboard"
    )
    log.info("serving dashboard", extra={"data": {"path": str(cfg.frontend_dist)}})


app = create_app()
