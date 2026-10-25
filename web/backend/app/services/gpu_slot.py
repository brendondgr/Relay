"""Taking GAIFA's GPU slot (plan 31.2; docs/mods.md "Taking the GPU"). The reference client: every Python mod
keeps an identical copy of this file (mods never import the hub), and it needs only httpx.

    async with gpu_slot("Image: portrait", est_s=90) as lease:
        ...                                   # the GPU work; pass lease.headers to a mod you hand work to

It asks the hub for the slot (`POST /api/gpu/acquire`) and waits its turn, renews the lease every 10 s while
the work runs, and gives it back on exit, on an exception and on task cancel. `on_wait(lease)` is called each
time the hub says the lease still waits (`lease.ahead`, `lease.eta_s`), for a job's `waiting_gpu` stage.

Without GAIFA (no GAIFA_URL), or when the hub can't be reached, the work runs at once (`lease.state == "off"`):
the slot orders work, it never stops a mod from working. A lease cancelled while it waits (the queue's Cancel)
raises `GpuCancelled`. Defaults come from the environment the hub sets: GAIFA_SERVICE_ID, and GAIFA_GPU_CLASS /
GAIFA_GPU_GROUP from the mod's `[gpu]` table.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger("gaifa.gpu_slot")

HEADER = "X-GAIFA-GPU-Lease"
RENEW_S = 10.0
POLL_S = 25.0


class GpuCancelled(Exception):
    """The lease was cancelled (or dropped) while it waited: don't run the work."""


@dataclass
class Lease:
    id: str | None = None
    state: str = "off"                  # "granted", "waiting", or "off" (no hub: ran without the slot)
    ahead: list[dict[str, Any]] = field(default_factory=list)
    eta_s: float | None = None
    label: str = ""

    @property
    def headers(self) -> dict[str, str]:
        """Pass down to a mod that does part of this work (it runs under this lease instead of queueing)."""
        return {HEADER: self.id} if self.id and self.state == "granted" else {}

    def waiting_text(self, name_of: Callable[[str], str] = lambda s: s) -> str:
        """"Waiting for the GPU: comfy Image (about 4 min)"."""
        first = self.ahead[0] if self.ahead else None
        what = f": {name_of(first['service'])} {first.get('label') or ''}".rstrip() if first else ""
        eta = f" (about {max(1, round(self.eta_s / 60))} min)" if self.eta_s else ""
        return f"Waiting for the GPU{what}{eta}"


def lease_from(headers: Any) -> str | None:
    """The parent lease a caller passed down (`X-GAIFA-GPU-Lease`), from request headers."""
    try:
        v = headers.get(HEADER) or headers.get(HEADER.lower())
    except AttributeError:
        return None
    return v.strip() or None if isinstance(v, str) else None


class GpuSlot:
    def __init__(self, hub_url: str | None = None, service: str | None = None, cls: str | None = None,
                 group: str | None = None, transport: httpx.AsyncBaseTransport | None = None,
                 renew_s: float = RENEW_S, poll_s: float = POLL_S) -> None:
        url = hub_url if hub_url is not None else os.environ.get("GAIFA_URL", "")
        self.url = (url or "").rstrip("/") or None
        self.service = service or os.environ.get("GAIFA_SERVICE_ID") or "mod"
        self.cls = cls or os.environ.get("GAIFA_GPU_CLASS") or "job"
        self.group = group or os.environ.get("GAIFA_GPU_GROUP") or None
        self.transport, self.renew_s, self.poll_s = transport, renew_s, poll_s

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.url or "", transport=self.transport,
                                 timeout=httpx.Timeout(10, read=self.poll_s + 15))

    @asynccontextmanager
    async def slot(self, label: str, est_s: float | None = None, cls: str | None = None, parent: str | None = None,
                   group: str | None = None, thread: str | None = None, call_id: str | None = None,
                   on_wait: Callable[[Lease], Awaitable[None] | None] | None = None) -> AsyncIterator[Lease]:
        body: dict[str, Any] = {"service": self.service, "class": cls or self.cls, "label": label}
        for k, v in (("group", group or self.group), ("est_s", est_s), ("parent", parent), ("thread", thread),
                     ("call_id", call_id)):
            if v is not None:
                body[k] = v
        lease = Lease(label=label)
        if not self.url:
            yield lease
            return
        lease.id = body["lease"] = "gl_" + secrets.token_hex(6)   # ours from the start: a cancel can always release it
        async with self._client() as c:
            renewer: asyncio.Task[None] | None = None
            try:
                await self._acquire(c, body, lease, on_wait)
                if lease.state == "granted":
                    renewer = asyncio.create_task(self._renew(c, body, lease))
                yield lease
            finally:
                if renewer:
                    renewer.cancel()
                    await asyncio.gather(renewer, return_exceptions=True)
                if lease.id:
                    # Shielded: a cancelled task still gives the slot back.
                    await asyncio.gather(asyncio.shield(self._release(c, lease.id)), return_exceptions=True)

    async def _acquire(self, c: httpx.AsyncClient, body: dict[str, Any], lease: Lease,
                       on_wait: Callable[[Lease], Awaitable[None] | None] | None) -> None:
        while True:
            asked = loop_time()
            try:
                r = await c.post("/api/gpu/acquire", params={"wait": self.poll_s}, json=body)
            except httpx.HTTPError as e:
                log.warning("GAIFA not reachable for the GPU slot (%s); running without it", e)
                lease.state, lease.id = "off", None
                return
            if r.status_code == 404:              # an older hub: no queue
                lease.state, lease.id = "off", None
                return
            try:
                data = r.json()
            except ValueError:
                data = {}
            if r.status_code >= 400 or not isinstance(data, dict):
                log.warning("GPU acquire answered %s: %s; running without the slot", r.status_code, r.text[:200])
                lease.state, lease.id = "off", None
                return
            state = data.get("state")
            if state == "granted":
                lease.state, lease.ahead, lease.eta_s = "granted", [], None
                return
            if state != "waiting":
                lease.id = None
                raise GpuCancelled(f"the GPU lease was {state or 'dropped'} while it waited")
            lease.state, lease.ahead, lease.eta_s = "waiting", data.get("ahead") or [], data.get("eta_s")
            if loop_time() - asked < 0.5:            # a hub that doesn't hold the request: don't spin
                await asyncio.sleep(0.5)
            if on_wait is not None:
                try:
                    res = on_wait(lease)
                    if inspect.isawaitable(res):
                        await res
                except Exception:  # noqa: BLE001: a status callback never stops the wait
                    log.exception("on_wait")

    async def _renew(self, c: httpx.AsyncClient, body: dict[str, Any], lease: Lease) -> None:
        info = {k: v for k, v in body.items() if k != "lease"}   # re-registers the lease after a hub restart
        while True:
            await asyncio.sleep(self.renew_s)
            try:
                r = await c.post(f"/api/gpu/{lease.id}/renew", json=info)
                if r.status_code == 202:
                    log.warning("the hub re-registered GPU lease %s as waiting (it restarted while another group "
                                "held the slot); the running work finishes", lease.id)
            except httpx.HTTPError as e:
                log.debug("GPU renew: %s", e)

    async def _release(self, c: httpx.AsyncClient, lease_id: str) -> None:
        try:
            await c.post(f"/api/gpu/{lease_id}/release", timeout=5)
        except httpx.HTTPError as e:
            log.warning("GPU release failed (%s): the hub frees it when the lease expires", e)


def loop_time() -> float:
    return asyncio.get_running_loop().time()


_default: GpuSlot | None = None


def gpu_slot(label: str, est_s: float | None = None, cls: str | None = None, parent: str | None = None,
             **kw: Any) -> Any:
    """`async with gpu_slot("label", est_s=…) as lease:` with the defaults from the environment."""
    global _default
    if _default is None:
        _default = GpuSlot()
    return _default.slot(label, est_s=est_s, cls=cls, parent=parent, **kw)
