"""Model facts for ``GET /v1/models``: context window, tools, vision, reasoning.

An agent has to know how much fits in a model's window, and whether the model
can call tools, see images or think out loud, *before* it sends a request.
The OpenAI ``/v1/models`` shape has no field for any of that, so relay adds a
``meta`` object to each entry, read from whatever the upstream will say:

1. **Override** — ``facts_override`` on the endpoint (set on the Endpoints
   page). Wins field by field: an operator who knows better is right.
2. **llama.cpp** — ``GET <origin>/props`` (origin = base URL without
   ``/v1``): ``default_generation_settings.n_ctx``, ``modalities.vision``,
   ``chat_template_caps.supports_tools`` (older builds: "tool" appears in
   the chat template), and reasoning when the template has a thinking switch.
3. **vLLM** — ``max_model_len`` on the model's entry in ``<base>/models``.
4. **Probe** — an upstream that is itself a relay (its catalog has
   relay-shaped entries) hides vLLM's catalog. One chat request with an
   absurd ``max_tokens`` makes vLLM name the limit in its 400
   (``max_model_len=131072`` or "maximum context length is N"). It is sent
   with ``stream: true`` and dropped on a 2xx, so a server that accepts it
   stops generating as soon as the connection closes.

Facts the upstream doesn't state stay ``null`` (unknown) rather than being
guessed. ``source`` names where ``context_length`` came from.

Why in the background: every OpenAI client calls ``/v1/models`` at start-up,
so it must answer quickly even when an upstream is slow or gone. A lookup
starts the detection, waits a short while for it, and answers with whatever
is known; a slow detection fills the cache for the next call. Results are
cached per (endpoint, base URL, model), so a changed URL or model is a new
key and a fresh read. A failed read is retried after ``retry_after``; a good
one is re-read after ``ttl`` while the old value keeps being served.

Alias-only protocols (OpenCode) are never read: their facts are whatever the
override says.
"""

import asyncio
import json
import re
import time
from dataclasses import dataclass

import httpx

from app.core.logging import get_logger

log = get_logger("facts")

FIELDS = ("context_length", "tools", "vision", "reasoning")
_PROBE_MAX_TOKENS = 10_000_000
_PROBE_BODY_LIMIT = 16_384

# Error texts that name a context limit. vLLM 0.x says
# "max_tokens=… cannot be greater than max_model_len=max_total_tokens=131072"
# (the alias chain varies by version); the OpenAI-style wording is "This
# model's maximum context length is 131072 tokens"; llama.cpp says "the
# request exceeds the available context size (4096 tokens)".
_LIMIT_PATTERNS = (
    re.compile(r"max_model_len(?:\s*=\s*[A-Za-z_]+)*\s*[=:]\s*(\d+)"),
    re.compile(r"maximum context length is\s+(\d+)", re.IGNORECASE),
    re.compile(r"available context size \((\d+)", re.IGNORECASE),
)
# A chat template that can switch thinking on (Gemma 4, Qwen 3, DeepSeek R1).
_THINKING_RE = re.compile(
    r"enable_thinking|<think>|thinking|reasoning_content", re.IGNORECASE)


def empty_meta() -> dict:
    return {**{f: None for f in FIELDS}, "source": None}


def parse_override(value) -> dict:
    """Read a ``facts_override`` cell (JSON text, dict, or NULL)."""
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def merge(detected: dict | None, override: dict | None) -> dict:
    """Detected facts with the override laid over them, field by field."""
    meta = empty_meta()
    if detected:
        meta.update({f: detected.get(f) for f in FIELDS})
        meta["source"] = detected.get("source")
    override = override or {}
    set_fields = [f for f in FIELDS if override.get(f) is not None]
    for f in set_fields:
        meta[f] = override[f]
    if "context_length" in set_fields or (set_fields and not meta["source"]):
        meta["source"] = "override"
    return meta


def parse_limit(text: str) -> int | None:
    """The context limit an upstream error message names, if any."""
    for pattern in _LIMIT_PATTERNS:
        m = pattern.search(text)
        if m:
            n = int(m.group(1))
            if n > 0:
                return n
    return None


def _positive_int(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value) if value > 0 else None


def facts_from_props(props: dict) -> dict | None:
    """llama.cpp ``/props`` -> facts, or None when it isn't llama.cpp's."""
    dgs = props.get("default_generation_settings")
    n_ctx = _positive_int((dgs or {}).get("n_ctx")
                          if isinstance(dgs, dict) else None)
    n_ctx = n_ctx or _positive_int(props.get("n_ctx"))
    if n_ctx is None:
        return None
    template = props.get("chat_template")
    template = template if isinstance(template, str) and template else None
    caps = props.get("chat_template_caps")
    if isinstance(caps, dict) and "supports_tools" in caps:
        tools = bool(caps["supports_tools"])
    else:
        tools = ("tool" in template) if template else None
    modalities = props.get("modalities")
    vision = (bool(modalities.get("vision"))
              if isinstance(modalities, dict) else None)
    reasoning = bool(_THINKING_RE.search(template)) if template else None
    return {"context_length": n_ctx, "tools": tools, "vision": vision,
            "reasoning": reasoning, "source": "props"}


def facts_from_catalog(payload, model: str | None) -> tuple[dict | None, bool]:
    """An upstream ``/models`` body -> (facts or None, looks like a relay).

    vLLM puts ``max_model_len`` on each entry. A relay (DashLLM) marks its
    entries with a ``relay`` object, which is the cue to probe instead.
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None, False
    entries = [e for e in data if isinstance(e, dict)]
    relay_like = any(isinstance(e.get("relay"), dict) for e in entries)
    wanted = (model or "").lower()
    entry = next((e for e in entries
                  if str(e.get("id", "")).lower() == wanted), None)
    if entry is None and len(entries) == 1:
        entry = entries[0]
    limit = _positive_int((entry or {}).get("max_model_len"))
    if limit is None:
        return None, relay_like
    return {"context_length": limit, "tools": None, "vision": None,
            "reasoning": None, "source": "vllm"}, relay_like


@dataclass
class _Entry:
    detected: dict | None = None  # last good read; kept through failures
    ok: bool = False              # did the most recent read find anything
    checked: float = 0.0          # time.monotonic() of the last finished read
    checked_ts: float | None = None  # wall clock of the same, for the API
    task: asyncio.Task | None = None


class ModelFacts:
    def __init__(self, router, http: httpx.AsyncClient, *, wait: float = 3.0,
                 request_timeout: float = 5.0, probe_timeout: float = 15.0,
                 retry_after: float = 300.0, ttl: float = 3600.0):
        self.router = router
        self.http = http
        self.wait = wait
        self.request_timeout = request_timeout
        self.probe_timeout = probe_timeout
        self.retry_after = retry_after
        self.ttl = ttl
        self._entries: dict[tuple[str, str, str], _Entry] = {}

    # ---- public -------------------------------------------------------------
    async def lookup(self, pairs: list[tuple[dict, str | None]]) -> list[dict]:
        """Facts for each (endpoint row, upstream model), in order.

        Starts any read that is due, waits up to ``self.wait`` for them, and
        answers with what is known; reads still running finish in the
        background. Cache entries for keys no longer listed are dropped.
        """
        keys = [self._key(row, model) for row, model in pairs]
        pending = {t for t in (self._ensure(row, model).task
                               for row, model in pairs if _readable(row))
                   if t is not None and not t.done()}
        if pending:
            await asyncio.wait(pending, timeout=self.wait)
        self._prune(set(keys))
        return [self.meta(row, model) for row, model in pairs]

    async def get(self, row: dict, model: str | None,
                  refresh: bool = False) -> dict:
        """One endpoint's facts, waiting for the read to finish. ``refresh``
        reads again even when the cached value is fresh."""
        if _readable(row):
            task = self._ensure(row, model, force=refresh).task
            if task is not None and not task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(task),
                                           timeout=self._read_budget())
                except asyncio.TimeoutError:
                    pass
        return self.state(row, model)

    def meta(self, row: dict, model: str | None) -> dict:
        entry = self._entries.get(self._key(row, model))
        return merge(entry.detected if entry else None,
                     parse_override(row.get("facts_override")))

    def state(self, row: dict, model: str | None) -> dict:
        """``meta`` plus what was detected before the override, for the
        admin route."""
        entry = self._entries.get(self._key(row, model))
        detected = merge(entry.detected if entry else None, None)
        return {"endpoint_id": row["id"], "model": model,
                "meta": self.meta(row, model), "detected": detected,
                "checked_ts": entry.checked_ts if entry else None}

    async def close(self) -> None:
        tasks = [e.task for e in self._entries.values()
                 if e.task is not None and not e.task.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ---- cache --------------------------------------------------------------
    @staticmethod
    def _key(row: dict, model: str | None) -> tuple[str, str, str]:
        return (row["id"], row["base_url"].rstrip("/"), model or "")

    def _ensure(self, row: dict, model: str | None,
                force: bool = False) -> _Entry:
        key = self._key(row, model)
        entry = self._entries.setdefault(key, _Entry())
        running = entry.task is not None and not entry.task.done()
        if not running and (force or self._due(entry)):
            entry.task = asyncio.create_task(
                self._read(entry, dict(row), model),
                name=f"facts:{row.get('name')}")
        return entry

    def _due(self, entry: _Entry) -> bool:
        if entry.checked == 0.0:
            return True
        age = time.monotonic() - entry.checked
        return age > (self.ttl if entry.ok else self.retry_after)

    def _prune(self, keep: set[tuple[str, str, str]]) -> None:
        for key in [k for k in self._entries if k not in keep]:
            entry = self._entries.pop(key)
            if entry.task is not None and not entry.task.done():
                entry.task.cancel()

    def _read_budget(self) -> float:
        return 2 * self.request_timeout + self.probe_timeout + 1.0

    # ---- reading the upstream ------------------------------------------------
    async def _read(self, entry: _Entry, row: dict, model: str | None) -> None:
        try:
            found = await asyncio.wait_for(
                self._detect(row, model), timeout=self._read_budget())
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — a bad upstream is not our crash
            log.warning("model facts read failed", extra={"data": {
                "endpoint": row.get("name"), "model": model,
                "error": f"{type(e).__name__}: {e}"[:300]}})
            found = None
        entry.ok = found is not None
        if found is not None:
            entry.detected = found
        entry.checked = time.monotonic()
        entry.checked_ts = time.time()
        log.info("model facts read", extra={"data": {
            "endpoint": row.get("name"), "model": model, "facts": found}})

    async def _detect(self, row: dict, model: str | None) -> dict | None:
        base = row["base_url"].rstrip("/")
        origin = base[:-3] if base.endswith("/v1") else base
        headers = {}
        if row.get("upstream_key"):
            headers["Authorization"] = f"Bearer {row['upstream_key']}"

        props = await self._get_json(origin + "/props", headers)
        if isinstance(props, dict):
            found = facts_from_props(props)
            if found:
                return found
        catalog = await self._get_json(base + "/models", headers)
        found, relay_like = facts_from_catalog(catalog, model)
        if found:
            return found
        if relay_like or row.get("server_type") == "vLLM":
            limit = await self._probe(base, model, headers)
            if limit:
                return {"context_length": limit, "tools": None,
                        "vision": None, "reasoning": None, "source": "probe"}
        return None

    async def _get_json(self, url: str, headers: dict):
        async def go():
            resp = await self.http.get(url, headers=headers,
                                       timeout=self.request_timeout)
            if resp.status_code != 200:
                return None
            try:
                return resp.json()
            except (json.JSONDecodeError, UnicodeDecodeError):
                return None
        # httpx.ASGITransport ignores timeouts, so the deadline is ours.
        try:
            return await asyncio.wait_for(go(), timeout=self.request_timeout)
        except (httpx.HTTPError, asyncio.TimeoutError):
            return None

    async def _probe(self, base: str, model: str | None,
                     headers: dict) -> int | None:
        body = {"model": model or "auto",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": _PROBE_MAX_TOKENS, "stream": True}

        async def go() -> int | None:
            async with self.http.stream(
                    "POST", base + "/chat/completions", json=body,
                    headers=headers, timeout=self.probe_timeout) as resp:
                if resp.status_code < 400:
                    return None  # accepted: closing the stream stops it
                raw = b""
                async for chunk in resp.aiter_bytes():
                    raw += chunk
                    if len(raw) >= _PROBE_BODY_LIMIT:
                        break
                return parse_limit(raw.decode(errors="replace"))

        try:
            return await asyncio.wait_for(go(), timeout=self.probe_timeout)
        except (httpx.HTTPError, asyncio.TimeoutError):
            return None


def _readable(row: dict) -> bool:
    """Only OpenAI-protocol upstreams are read; others get the override."""
    return (row.get("protocol") or "openai") == "openai"
