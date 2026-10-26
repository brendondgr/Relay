"""Control plane: endpoint CRUD, hot-swap, live tests, router policy."""

import time

from fastapi import APIRouter, HTTPException, Request

from app.core.logging import get_logger
from app.schemas import (
    EndpointCreate,
    EndpointOut,
    EndpointPatch,
    EndpointTestResult,
    ModelFactsOut,
    RouterState,
    RouterUpdate,
    SshHostOut,
    TunnelRespond,
    TunnelRouteCreate,
    TunnelRouteOut,
    TunnelRoutePatch,
    TunnelRouteTestResult,
    TunnelSessionStatus,
)
from app.services import ssh_config
from app.services.tunnel_sessions import probe_route

log = get_logger("admin")

router = APIRouter(prefix="/admin", tags=["endpoints"])


async def _shares(request: Request) -> dict[str, float]:
    """Each endpoint's fraction of requests over the last 24h."""
    rows = await request.app.state.db.aquery(
        "SELECT endpoint_id, COUNT(*) AS n FROM requests"
        " WHERE ts > ? GROUP BY endpoint_id", (time.time() - 86400,))
    total = sum(r["n"] for r in rows) or 1
    return {r["endpoint_id"]: r["n"] / total for r in rows if r["endpoint_id"]}


@router.get("/endpoints", response_model=list[EndpointOut])
async def list_endpoints(request: Request):
    return request.app.state.router.list_out(await _shares(request))


@router.post("/endpoints", response_model=EndpointOut, status_code=201)
async def create_endpoint(request: Request, spec: EndpointCreate):
    if not spec.base_url and not spec.preset:
        raise HTTPException(422, "base_url is required (or a preset: anthropic, openai, gemini)")
    if spec.base_url and not spec.base_url.startswith(("http://", "https://")):
        raise HTTPException(422, "base_url must start with http:// or https://")
    try:
        row = request.app.state.router.create(spec)
    except ValueError as e:
        raise HTTPException(422, str(e))
    # Probe immediately so the UI shows real health, not 'unknown'.
    await request.app.state.prober._probe(row["id"])
    return request.app.state.router.out(row["id"])


@router.patch("/endpoints/{eid}", response_model=EndpointOut)
async def patch_endpoint(request: Request, eid: str, patch: EndpointPatch):
    try:
        row = request.app.state.router.patch(eid, patch)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if row is None:
        raise HTTPException(404, "endpoint not found")
    return request.app.state.router.out(eid)


@router.delete("/endpoints/{eid}", status_code=204)
async def delete_endpoint(request: Request, eid: str):
    if not request.app.state.router.delete(eid):
        raise HTTPException(404, "endpoint not found")


@router.post("/endpoints/{eid}/activate", response_model=RouterState)
async def activate_endpoint(request: Request, eid: str):
    if not request.app.state.router.activate(eid):
        raise HTTPException(404, "endpoint not found")
    return request.app.state.router.router_state(
        request.app.state.settings.current.auto_failover)


@router.post("/endpoints/{eid}/test", response_model=EndpointTestResult)
async def test_endpoint(request: Request, eid: str):
    result = await request.app.state.prober.probe_endpoint(eid)
    if result is None:
        raise HTTPException(404, "endpoint not found")
    log.info("manual endpoint test", extra={"data": {
        "id": eid, "ok": result.ok, "latency_ms": result.latency_ms}})
    # Feed the manual test into the health state machine too.
    if result.ok:
        request.app.state.router.report_success(
            eid, result.latency_ms, source="probe")
        if result.models:
            request.app.state.router.set_models(eid, result.models)
    else:
        request.app.state.router.report_failure(
            eid, result.error or "test failed", source="probe")
    return result


@router.get("/endpoints/{eid}/facts", response_model=ModelFactsOut)
async def endpoint_facts(request: Request, eid: str, refresh: bool = False,
                         model: str | None = None):
    """Model facts for the endpoint's upstream model (or ``model``), as
    ``/v1/models`` shows them, plus what was detected before the override.
    ``refresh=1`` reads the upstream again now and waits for it."""
    row = request.app.state.router.endpoints.get(eid)
    if row is None:
        raise HTTPException(404, "endpoint not found")
    model = model or request.app.state.router.upstream_model(eid)
    return await request.app.state.facts.get(row, model, refresh=refresh)


# ---- interactive SSH tunnel sessions --------------------------------------
# Manual connect/disconnect for tunnel-backed endpoints. Nothing connects on
# its own; ssh runs in a PTY so prompts (host key, passphrase, password) are
# surfaced to the UI and answered via /respond.

@router.get("/endpoints/tunnel-sessions",
            response_model=list[TunnelSessionStatus])
async def tunnel_sessions(request: Request):
    return request.app.state.tunnel_sessions.all_status()


def _tunnel_endpoint(request: Request, eid: str) -> dict:
    row = request.app.state.router.endpoints.get(eid)
    if row is None:
        raise HTTPException(404, "endpoint not found")
    return row


@router.post("/endpoints/{eid}/tunnel/connect",
             response_model=TunnelSessionStatus)
async def tunnel_connect(request: Request, eid: str):
    row = _tunnel_endpoint(request, eid)
    command = (row.get("tunnel_command") or "").strip()
    if not command:
        raise HTTPException(
            422, "endpoint has no tunnel_command; add one to connect")
    try:
        return await request.app.state.tunnel_sessions.connect(
            eid, command, row.get("tunnel_local_port"))
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.post("/endpoints/{eid}/tunnel/disconnect",
             response_model=TunnelSessionStatus)
async def tunnel_disconnect(request: Request, eid: str):
    _tunnel_endpoint(request, eid)
    return await request.app.state.tunnel_sessions.disconnect(eid)


@router.post("/endpoints/{eid}/tunnel/respond",
             response_model=TunnelSessionStatus)
async def tunnel_respond(request: Request, eid: str, body: TunnelRespond):
    _tunnel_endpoint(request, eid)
    out = await request.app.state.tunnel_sessions.respond(eid, body.text)
    if out is None:
        raise HTTPException(409, "no active session awaiting input")
    return out


@router.get("/endpoints/{eid}/tunnel", response_model=TunnelSessionStatus)
async def tunnel_status(request: Request, eid: str):
    _tunnel_endpoint(request, eid)
    return request.app.state.tunnel_sessions.status(eid)


# ---- ssh tunnel routes (multiple candidate commands per endpoint) --------
# An endpoint keeps one alias/base_url; each route is a saved ssh command
# that can be activated (copied into tunnel_command) or quick-probed without
# opening a real tunnel, so a flaky route can be swapped for a working one.

@router.get("/endpoints/{eid}/routes", response_model=list[TunnelRouteOut])
async def list_routes(request: Request, eid: str):
    _tunnel_endpoint(request, eid)
    return request.app.state.router.list_routes(eid)


@router.post("/endpoints/{eid}/routes", response_model=TunnelRouteOut,
             status_code=201)
async def create_route(request: Request, eid: str, spec: TunnelRouteCreate):
    _tunnel_endpoint(request, eid)
    try:
        return request.app.state.router.create_route(
            eid, spec.label, spec.command)
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.patch("/endpoints/{eid}/routes/{rid}", response_model=TunnelRouteOut)
async def patch_route(request: Request, eid: str, rid: str,
                       patch: TunnelRoutePatch):
    _tunnel_endpoint(request, eid)
    try:
        row = request.app.state.router.patch_route(
            eid, rid, patch.label, patch.command)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if row is None:
        raise HTTPException(404, "route not found")
    return row


@router.delete("/endpoints/{eid}/routes/{rid}", status_code=204)
async def delete_route(request: Request, eid: str, rid: str):
    _tunnel_endpoint(request, eid)
    if not request.app.state.router.delete_route(eid, rid):
        raise HTTPException(404, "route not found")


@router.post("/endpoints/{eid}/routes/{rid}/activate",
             response_model=EndpointOut)
async def activate_route(request: Request, eid: str, rid: str):
    _tunnel_endpoint(request, eid)
    if request.app.state.router.activate_route(eid, rid) is None:
        raise HTTPException(404, "route not found")
    return request.app.state.router.out(eid)


@router.post("/endpoints/{eid}/routes/{rid}/test",
             response_model=TunnelRouteTestResult)
async def test_route(request: Request, eid: str, rid: str):
    _tunnel_endpoint(request, eid)
    row = request.app.state.router.get_route(eid, rid)
    if row is None:
        raise HTTPException(404, "route not found")
    result = await probe_route(row["command"])
    log.info("tunnel route test", extra={"data": {
        "endpoint": eid, "route": rid, **result}})
    return result


# ---- ssh config hosts ------------------------------------------------------
# Read the user's ~/.ssh/config so shorthand tunnel commands (e.g.
# `ssh -N -L 9090:localhost:9090 skynet-alt`) can be built from real aliases,
# and so the UI shows the actual IdentityFile ssh will use — not a guess.

@router.get("/ssh/hosts", response_model=list[SshHostOut])
async def ssh_hosts(request: Request):
    return await ssh_config.list_hosts()


@router.get("/ssh/resolve", response_model=SshHostOut)
async def ssh_resolve(request: Request, host: str):
    try:
        return await ssh_config.resolve(host)
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.get("/endpoints/health")
async def pool_health(request: Request):
    r = request.app.state.router
    return {
        "endpoints": [
            {"id": eid, "name": r.endpoints[eid]["name"],
             "health": st.health, "consecutive_fails": st.consecutive_fails,
             "ewma_latency_ms": st.ewma_latency_ms, "model": st.model}
            for eid, st in r.state.items()
        ],
        "resolved": (r.resolve(
            auto_failover=request.app.state.settings.current.auto_failover
        ) or {}).get("id"),
    }


@router.get("/router", response_model=RouterState)
async def get_router(request: Request):
    return request.app.state.router.router_state(
        request.app.state.settings.current.auto_failover)


@router.put("/router", response_model=RouterState)
async def put_router(request: Request, update: RouterUpdate):
    try:
        request.app.state.router.set_policy(update.policy, update.pinned_id)
    except KeyError:
        raise HTTPException(404, "pinned endpoint not found")
    return request.app.state.router.router_state(
        request.app.state.settings.current.auto_failover)
