"""Live model lists: what each endpoint serves, read from the endpoint itself.

Never a hardcoded list. Each protocol asks its own way:

- **anthropic** — the official SDK's ``models.list()``: context window
  (``max_input_tokens``), output cap (``max_tokens``), image input, thinking and
  the exact effort ladder each model accepts. Retired models are left out.
- **gemini** — the official ``google-genai`` SDK's ``models.list()``: the
  ``models/`` prefix stripped, only models that ``generateContent``, with their
  input/output limits and whether they think.
- **openai** — ``GET {base}/models``: ids and nothing else (OpenAI says nothing
  more). A cloud OpenAI list mixes chat with embedding, speech, image and
  moderation models, so a *kind* filter hides ids naming those (a rule about
  kinds, not a list of names; ``hide_filter`` turns it off).

Lists are cached per endpoint (``ttl``, 10 minutes) and refreshed on demand.
Discovery never raises into a caller: a failure keeps the last good list and
says why (``error``). A connect timeout is short (2 s) so a server that isn't
running fails fast; a slow listing gets 15 s.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import asdict, dataclass, field

import httpx

from app.core.logging import get_logger

log = get_logger("discovery")

# Ids of non-chat models in an OpenAI-style list (the hide filter).
NOT_CHAT = re.compile(r"embed|whisper|tts|transcribe|dall-e|image|moderation|realtime|audio|search|babbage|davinci|sora", re.I)
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


@dataclass
class Listed:
    id: str
    display_name: str | None = None
    context_length: int | None = None
    max_output: int | None = None
    tools: bool | None = None
    vision: bool | None = None
    reasoning: bool | None = None
    effort: list[str] | None = None       # the effort ladder the model accepts (anthropic)
    thinking_types: list[str] | None = None   # e.g. ["adaptive"], ["enabled"] (anthropic)
    source: str = "server"

    def facts(self) -> dict:
        return {"context_length": self.context_length, "tools": self.tools, "vision": self.vision,
                "reasoning": self.reasoning}


@dataclass
class Listing:
    models: list[Listed] = field(default_factory=list)
    ts: float | None = None
    error: str | None = None


class DiscoveryError(Exception):
    pass


def listed_models(row: dict) -> bool:
    """Whether the catalog lists this endpoint's models one by one (`<alias>/<model>`)."""
    flag = row.get("list_models")
    if flag is None:
        return row.get("kind") == "cloud" or (row.get("protocol") or "openai") in ("anthropic", "gemini")
    return bool(flag)


def _anthropic(m) -> Listed:
    caps = getattr(m, "capabilities", None)
    def sup(name):
        c = getattr(caps, name, None) if caps else None
        return bool(getattr(c, "supported", False)) if c is not None else None
    effort = None
    eff = getattr(caps, "effort", None) if caps else None
    if eff is not None and getattr(eff, "supported", False):
        effort = [lvl for lvl in EFFORT_LEVELS if getattr(getattr(eff, lvl, None), "supported", False)]
    thinking_types = None
    th = getattr(caps, "thinking", None) if caps else None
    if th is not None and getattr(th, "types", None) is not None:
        types = th.types
        items = types.model_dump() if hasattr(types, "model_dump") else dict(types)
        thinking_types = [k for k, v in items.items() if isinstance(v, dict) and v.get("supported")]
    return Listed(id=m.id, display_name=getattr(m, "display_name", None), context_length=getattr(m, "max_input_tokens", None),
                  max_output=getattr(m, "max_tokens", None), tools=True, vision=sup("image_input"), reasoning=sup("thinking"),
                  effort=effort, thinking_types=thinking_types)


def _gemini(m) -> Listed | None:
    actions = getattr(m, "supported_actions", None) or []
    if "generateContent" not in actions:
        return None
    name = (getattr(m, "name", "") or "").removeprefix("models/")
    return Listed(id=name, display_name=getattr(m, "display_name", None), context_length=getattr(m, "input_token_limit", None),
                  max_output=getattr(m, "output_token_limit", None), tools=True, reasoning=getattr(m, "thinking", None))


class Discovery:
    def __init__(self, router, http: httpx.AsyncClient, ttl: float = 600.0) -> None:
        self.router = router
        self.http = http
        self.ttl = ttl
        self.cache: dict[str, Listing] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ---- per protocol ------------------------------------------------------
    async def _list_anthropic(self, row: dict) -> list[Listed]:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=row.get("upstream_key") or "", base_url=row["base_url"], max_retries=0,
                                          timeout=anthropic.Timeout(15.0, connect=2.0))
        try:
            out = []
            async for m in client.models.list(limit=1000):
                if getattr(m, "lifecycle", "active") == "retired":
                    continue
                out.append(_anthropic(m))
            return out
        except anthropic.APIStatusError as e:
            raise DiscoveryError(_status_text(e.status_code, getattr(e, "message", str(e)))) from e
        except anthropic.APIError as e:
            raise DiscoveryError(f"{type(e).__name__}: {e}") from e
        finally:
            await client.close()

    async def _list_gemini(self, row: dict) -> list[Listed]:
        from google import genai
        from google.genai import errors, types
        client = genai.Client(api_key=row.get("upstream_key") or "", http_options=types.HttpOptions(
            base_url=row["base_url"], timeout=15000))
        try:
            out = []
            async for m in await client.aio.models.list(config={"page_size": 1000}):
                item = _gemini(m)
                if item:
                    out.append(item)
            return out
        except errors.APIError as e:
            raise DiscoveryError(_status_text(getattr(e, "code", None), getattr(e, "message", None) or str(e))) from e
        except (httpx.HTTPError, OSError) as e:
            raise DiscoveryError(f"{type(e).__name__}: {e}") from e

    async def _list_openai(self, row: dict) -> list[Listed]:
        headers = {"Authorization": f"Bearer {row['upstream_key']}"} if row.get("upstream_key") else {}
        try:
            r = await self.http.get(row["base_url"].rstrip("/") + "/models", headers=headers,
                                    timeout=httpx.Timeout(15.0, connect=2.0))
        except httpx.HTTPError as e:
            raise DiscoveryError(f"{type(e).__name__}: {e}") from e
        if r.status_code != 200:
            try:
                msg = (r.json().get("error") or {}).get("message")
            except ValueError:
                msg = None
            raise DiscoveryError(_status_text(r.status_code, msg or r.text[:200]))
        try:
            data = r.json().get("data") or []
        except (ValueError, AttributeError) as e:
            raise DiscoveryError("the model list isn't JSON") from e
        out = []
        for m in data:
            mid = m.get("id") if isinstance(m, dict) else None
            if not mid:
                continue
            ctx = m.get("max_model_len") or (m.get("meta") or {}).get("n_ctx_train")   # vLLM, llama.cpp
            out.append(Listed(id=mid, context_length=ctx if isinstance(ctx, int) else None, source="server"))
        return out

    async def _list(self, row: dict) -> list[Listed]:
        protocol = row.get("protocol") or "openai"
        if protocol == "anthropic":
            return await self._list_anthropic(row)
        if protocol == "gemini":
            return await self._list_gemini(row)
        if protocol == "openai":
            return await self._list_openai(row)
        raise DiscoveryError(f"{protocol} endpoints don't list models here")

    # ---- the cache ---------------------------------------------------------
    async def listing(self, eid: str, refresh: bool = False) -> Listing:
        row = self.router.endpoints.get(eid)
        if row is None:
            return Listing(error="no such endpoint")
        cached = self.cache.get(eid)
        if cached and cached.ts and not refresh and time.time() - cached.ts < self.ttl:
            return cached
        lock = self._locks.setdefault(eid, asyncio.Lock())
        async with lock:
            cached = self.cache.get(eid)
            if cached and cached.ts and not refresh and time.time() - cached.ts < self.ttl:
                return cached
            try:
                models = await self._list(row)
                listing = Listing(models=models, ts=time.time())
            except DiscoveryError as e:
                listing = Listing(models=cached.models if cached else [], ts=time.time(), error=str(e))
                log.warning("model list failed", extra={"data": {"endpoint": row["name"], "error": str(e)[:200]}})
            except Exception as e:  # noqa: BLE001 — discovery never raises into a caller
                listing = Listing(models=cached.models if cached else [], ts=time.time(), error=f"{type(e).__name__}: {e}")
                log.exception("model list failed", extra={"data": {"endpoint": row["name"]}})
            self.cache[eid] = listing
            return listing

    def hidden(self, row: dict, model_id: str) -> bool:
        if model_id in set(self.router.hidden_models(row["id"])):
            return True
        return bool(row.get("hide_filter", 1)) and row.get("kind") == "cloud" and (row.get("protocol") or "openai") == "openai" \
            and bool(NOT_CHAT.search(model_id))

    async def visible(self, eid: str, refresh: bool = False) -> list[Listed]:
        row = self.router.endpoints.get(eid)
        if row is None:
            return []
        return [m for m in (await self.listing(eid, refresh)).models if not self.hidden(row, m.id)]

    def find(self, eid: str, model_id: str) -> Listed | None:
        """The cached entry of one model (no I/O): what an adapter needs (max_tokens, effort ladder)."""
        listing = self.cache.get(eid)
        return next((m for m in listing.models if m.id == model_id), None) if listing else None

    def view(self, eid: str, listing: Listing) -> dict:
        row = self.router.endpoints[eid]
        return {"endpoint": row["name"], "alias": row.get("alias"), "ts": listing.ts, "error": listing.error,
                "hide_filter": bool(row.get("hide_filter", 1)),
                "models": [{**asdict(m), "hidden": self.hidden(row, m.id)} for m in listing.models]}

    async def refresh_listed(self) -> None:
        """Refresh every endpoint whose models the catalog lists (in the background, at start)."""
        for eid, row in list(self.router.endpoints.items()):
            if row["enabled"] and listed_models(row):
                await self.listing(eid)


def _status_text(status: int | None, message: str | None) -> str:
    what = {401: "key rejected", 403: "no permission", 404: "not found", 429: "rate limited"}.get(status or 0)
    return f"HTTP {status}{': ' + what if what else ''}{' — ' + message if message else ''}"
