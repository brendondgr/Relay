"""AIHub's GPU slot per request to a local engine (AIHub plan 31.4).

A request routed to an LLM server on this machine (an OpenAI-protocol endpoint whose kind is ``local``
and whose URL is loopback: lcpp's llama-servers, vllm) takes an ``interactive`` lease from the hub
before it is forwarded, and holds it for the whole request: a stream holds it until its last chunk.
Requests to one engine share the slot (their group is the endpoint's registration key, e.g.
``lcpp:8080``, or ``relay:<endpoint id>``), so the engine still batches them; another group's work (a
ComfyUI image, a Whisper transcription) waits for them, and they wait for it. Remote endpoints (a
tunnel such as skynet, or any other host) and agent protocols (OpenCode) never take it.

The lease's ``service`` is the mod that owns the endpoint (``lcpp``, ``vllm``) or ``relay``; its
``label`` is the model; its ``thread`` is the caller's ``X-AIHub-Thread`` (the agent sends its chat
id, so it can show "Waiting for the GPU" on the turn). A caller that already holds the slot (a mod's job
asking a local model for something) passes its lease in ``X-AIHub-GPU-Lease`` and its request runs
under it instead of waiting behind itself.

Without AIHub (no AIHUB_URL), or with the ``gpu_slot`` setting off, nothing waits.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.core.logging import get_logger
from app.services.gpu_slot import GpuCancelled, GpuSlot, lease_from

log = get_logger("gpu")

THREAD_HEADER = "x-aihub-thread"
WITHIN_HEADER = "x-aihub-answer-within"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}

__all__ = ["GpuCancelled", "GpuLeases", "THREAD_HEADER", "WITHIN_HEADER", "answer_within", "lease_from", "lease_spec"]


def answer_within(headers: Any) -> float | None:
    """`X-AIHub-Answer-Within: <seconds>`: how long the caller gives the model once the GPU slot is held, or None."""
    try:
        s = float(headers.get(WITHIN_HEADER) or "")
    except ValueError:
        return None
    return s if 0 < s <= 3600 else None


def lease_spec(endpoint: dict) -> dict[str, str] | None:
    """`{service, group}` when a request to this endpoint takes the GPU slot, else None."""
    if (endpoint.get("protocol") or "openai") != "openai" or endpoint.get("kind") != "local":
        return None
    if endpoint.get("tunnel_id") or (endpoint.get("tunnel_command") or "").strip():
        return None
    host = (urlsplit(endpoint.get("base_url") or "").hostname or "").lower()
    if host not in LOOPBACK and not host.startswith("127."):
        return None
    return {"service": endpoint.get("owner") or "relay",
            "group": endpoint.get("external_key") or f"relay:{endpoint.get('id')}"}


class GpuLeases:
    """The leases of the requests in flight, by request id."""

    def __init__(self, hub_url: str | None = None, transport: httpx.AsyncBaseTransport | None = None,
                 poll_s: float = 25.0) -> None:
        self.hub_url, self.transport, self.poll_s = hub_url, transport, poll_s
        self._slots: dict[str, GpuSlot] = {}
        self._held: dict[str, Any] = {}          # req_id → the entered context manager
        self._tasks: set[asyncio.Task] = set()

    def _slot(self, service: str) -> GpuSlot:
        s = self._slots.get(service)
        if s is None:
            s = self._slots[service] = GpuSlot(self.hub_url, service=service, cls="interactive",
                                               transport=self.transport, poll_s=self.poll_s)
        return s

    @property
    def enabled(self) -> bool:
        return bool(self._slot("relay").url)

    def holds(self, req_id: str) -> bool:
        return req_id in self._held

    async def take(self, req_id: str, endpoint: dict, model: str | None, thread: str | None = None,
                   parent: str | None = None) -> bool:
        """Wait for the slot before forwarding to `endpoint`; True when a lease is held. A retry on another
        endpoint (failover) gives the previous one back first. Raises GpuCancelled when the queue's Cancel
        dropped it while it waited."""
        await self.release(req_id)
        spec = lease_spec(endpoint)
        if spec is None or not self.enabled:
            return False
        cm = self._slot(spec["service"]).slot(model or endpoint.get("name") or "LLM request", cls="interactive",
                                              group=spec["group"], thread=(thread or "")[:64] or None,
                                              parent=parent)
        lease = await cm.__aenter__()        # waits; a cancel here releases inside the client
        if lease.state != "granted":         # no hub, or it didn't answer: ran without the slot
            await cm.__aexit__(None, None, None)
            return False
        self._held[req_id] = cm
        return True

    async def release(self, req_id: str) -> None:
        cm = self._held.pop(req_id, None)
        if cm is not None:
            try:
                await cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001: the hub frees it when the lease expires
                log.exception("releasing the GPU lease of %s", req_id)

    def finish(self, req_id: str) -> None:
        """From the sync `ProxyService.finish`: give the lease back in the background."""
        if req_id not in self._held:
            return
        t = asyncio.get_running_loop().create_task(self.release(req_id))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        for req_id in list(self._held):
            await self.release(req_id)
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
