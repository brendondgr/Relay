"""The OpenAI-compatible adapter: forward verbatim, probe ``GET /models``.

This is relay's original behavior, extracted unchanged so the protocol seam
could be introduced without altering the hot path for every existing endpoint.
Nothing here is OpenCode-aware.
"""

import json
import time

import httpx
from fastapi import Request
from fastapi.responses import Response

from app.core.logging import get_logger
from app.schemas import EndpointTestResult
from app.services.adapters.base import (
    HOP_BY_HOP,
    RetryableUpstreamError,
    StaleModelOverride,
    UpstreamAdapter,
    classify_httpx_error,
)
from app.services.telemetry import RequestRecord

log = get_logger("health")


def _model_ids(payload) -> list[str]:
    try:
        return [m.get("id", "?") for m in payload.get("data", [])]
    except AttributeError:
        return []


# Fields relay adds for the translated protocols; OpenAI's own API rejects unknown ones.
_OURS = ("reasoning_details", "reasoning_content")


def cloud_body(body: bytes) -> bytes:
    """OpenAI's API (GAIFA plan llm-servers 1.6): `max_tokens` is `max_completion_tokens` there, a
    stream always reports usage, and the reasoning fields relay hands out for other providers go."""
    try:
        data = json.loads(body)
    except ValueError:
        return body
    if not isinstance(data, dict):
        return body
    if "max_tokens" in data and "max_completion_tokens" not in data:
        data["max_completion_tokens"] = data.pop("max_tokens")
    if data.get("stream"):
        data["stream_options"] = {**(data.get("stream_options") or {}), "include_usage": True}
    for m in data.get("messages") or []:
        if isinstance(m, dict):
            for k in _OURS:
                m.pop(k, None)
    return json.dumps(data).encode()


def _unsupported(content: bytes, body: bytes) -> tuple[str, bytes] | None:
    """A 400 naming a parameter this model doesn't take → (its name, the body without it)."""
    try:
        err = json.loads(content).get("error") or {}
        data = json.loads(body)
    except (ValueError, AttributeError):
        return None
    param = err.get("param")
    code = err.get("code") or ""
    if not param or code not in ("unsupported_parameter", "unsupported_value") or param not in data:
        return None
    data.pop(param)
    return param, json.dumps(data).encode()


class PassthroughAdapter(UpstreamAdapter):
    name = "openai"

    async def probe(self, http: httpx.AsyncClient, endpoint: dict,
                    timeout: float) -> EndpointTestResult:
        url = endpoint["base_url"] + "/models"
        headers = {}
        if endpoint.get("upstream_key"):
            headers["Authorization"] = f"Bearer {endpoint['upstream_key']}"
        t0 = time.perf_counter()
        try:
            resp = await http.get(url, headers=headers, timeout=timeout)
            latency = (time.perf_counter() - t0) * 1000
            if resp.status_code == 200:
                models = _model_ids(resp.json())
                log.debug("probe ok", extra={"data": {
                    "endpoint": endpoint["name"],
                    "latency_ms": round(latency, 1), "models": len(models)}})
                return EndpointTestResult(
                    ok=True, latency_ms=round(latency, 1), models=models)
            return EndpointTestResult(
                ok=False, latency_ms=round(latency, 1),
                error=f"HTTP {resp.status_code}")
        except httpx.HTTPError as e:
            latency = (time.perf_counter() - t0) * 1000
            return EndpointTestResult(
                ok=False, latency_ms=round(latency, 1),
                error=f"{type(e).__name__}: {e}")

    async def forward(self, proxy, request: Request, endpoint: dict, path: str,
                      record: RequestRecord, body: bytes,
                      t0: float) -> Response:
        url = endpoint["base_url"] + "/" + path.lstrip("/")
        headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in HOP_BY_HOP
        }
        if endpoint.get("upstream_key"):
            headers["Authorization"] = f"Bearer {endpoint['upstream_key']}"

        cloud = endpoint.get("kind") == "cloud" and body
        if cloud:
            body = cloud_body(body)
        dropped: list[str] = []
        for _ in range(4):
            upstream_req = proxy.http.build_request(
                request.method, url, headers=headers,
                content=body if body else None)
            try:
                resp = await proxy.http.send(upstream_req, stream=True)
            except httpx.HTTPError as e:
                raise classify_httpx_error(e) from e
            if not (cloud and resp.status_code == 400):
                break
            content = await resp.aread()
            retry = _unsupported(content, body)
            if retry is None:
                kept = {k: v for k, v in resp.headers.items()                # already decoded: no encoding headers
                        if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")}
                resp = httpx.Response(400, content=content, headers=kept, request=upstream_req)
                break
            await resp.aclose()
            dropped.append(retry[0])
            body = retry[1]

        if resp.status_code >= 500:
            text = (await resp.aread())[:1000]
            await resp.aclose()
            if proxy._is_stale_override(endpoint, record, text):
                raise StaleModelOverride(endpoint["model_override"])
            raise RetryableUpstreamError(
                f"HTTP {resp.status_code}: "
                f"{text[:300].decode(errors='replace')}",
                status=resp.status_code)

        if record.stream and resp.headers.get(
                "content-type", "").startswith("text/event-stream"):
            out = proxy._stream_response(resp, endpoint, record, t0)
        else:
            out = await proxy._buffered_response(resp, endpoint, record, t0)
        if dropped:
            out.headers["X-Relay-Dropped"] = ",".join(dropped)
        return out
