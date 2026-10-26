"""Telemetry capture: one row per proxied request, written off the hot path.

- ``RequestRecord``: the finished-request row (metadata only by default).
- ``TelemetryWriter``: asyncio queue -> batched SQLite inserts + retention
  pruning; the proxy never waits on a disk write.
- ``LiveTracker``: in-memory registry of in-flight requests + a rolling
  concurrency series for the live dashboard widgets.
"""

import asyncio
import logging
import time
import zlib
from collections import deque

from pydantic import BaseModel

from app.core.logging import get_logger
from app.db import Database
from app.services.rollup import HIST_UPSERT, SUM_UPSERT, build_rollup_rows

log = get_logger("telemetry")

_STOP = object()

# Records the writer will hold before it starts dropping, and how many it
# folds into one transaction. Deep enough to absorb a burst of a few
# thousand requests while the previous batch is still committing.
_QUEUE_DEPTH = 50_000
_BATCH_SIZE = 500

# Rows deleted per retention pass before the writer gets the lock back.
_PRUNE_CHUNK = 5_000

# Seconds between "telemetry queue full" reports. Overflow happens exactly
# when the process is already struggling; one log line per dropped record
# turns a hiccup into a log storm that makes it worse.
_DROP_LOG_INTERVAL = 10.0

# Request bodies (prompts/completions) are natural-language / JSON text that
# compresses ~3-5x. We store them zlib-deflated as BLOBs so "log bodies" +
# infinite retention stays affordable. Reads go through ``body_unpack`` which
# is backward compatible with pre-compression rows stored as plain TEXT.
_ZLIB_MAGIC = 0x78  # first byte of a zlib stream (CMF, window<=32K, level 6)


def body_pack(text: str | None) -> bytes | None:
    if text is None:
        return None
    return zlib.compress(text.encode("utf-8"), 6)


def body_unpack(value) -> str | None:
    """Inverse of :func:`body_pack`. Tolerates legacy uncompressed TEXT rows
    and already-decoded ``str`` values so it is safe on any historical row."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        if len(value) and value[0] == _ZLIB_MAGIC:
            try:
                return zlib.decompress(value).decode("utf-8")
            except zlib.error:
                pass  # not actually zlib -> fall through and decode as-is
        return bytes(value).decode("utf-8", "replace")
    return str(value)


class RequestRecord(BaseModel):
    id: str
    ts: float
    endpoint_id: str | None = None
    endpoint_name: str | None = None
    route: str = "unknown"
    model: str | None = None
    client_key: str | None = None  # masked (…last4)
    stream: bool = False
    status: int | None = None
    ok: bool = False
    error: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    ttft_ms: float | None = None
    latency_ms: float | None = None
    tokens_per_sec: float | None = None
    cost_usd: float | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    grp: str | None = None                 # local, remote, cloud, agent (catalog.group)
    cached_tokens: int | None = None       # prompt tokens read from the provider's cache
    cache_write_tokens: int | None = None  # prompt tokens written to it (Anthropic bills them apart)
    # bodies are only persisted when the log_bodies setting is on
    prompt_body: str | None = None
    completion_body: str | None = None


_INSERT = """
INSERT OR REPLACE INTO requests (
  id, ts, endpoint_id, endpoint_name, route, model, client_key, stream,
  status, ok, error, prompt_tokens, completion_tokens, total_tokens,
  ttft_ms, latency_ms, tokens_per_sec, cost_usd, temperature, max_tokens,
  grp, cached_tokens, cache_write_tokens
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

_INSERT_BODY = (
    "INSERT OR REPLACE INTO request_bodies (id, prompt, completion) VALUES (?,?,?)"
)


def _row(r: RequestRecord) -> tuple:
    return (
        r.id, r.ts, r.endpoint_id, r.endpoint_name, r.route, r.model,
        r.client_key, int(r.stream), r.status, int(r.ok), r.error,
        r.prompt_tokens, r.completion_tokens, r.total_tokens, r.ttft_ms,
        r.latency_ms, r.tokens_per_sec, r.cost_usd, r.temperature,
        r.max_tokens, r.grp, r.cached_tokens, r.cache_write_tokens,
    )


class TelemetryWriter:
    def __init__(self, db: Database):
        self.db = db
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_DEPTH)
        self._task: asyncio.Task | None = None
        self._last_prune = 0.0
        self.written = 0
        self.dropped = 0
        self._last_drop_log = 0.0

    def submit(self, record: RequestRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except asyncio.QueueFull:  # pragma: no cover - backpressure guard
            self.dropped += 1
            now = time.monotonic()
            if now - self._last_drop_log >= _DROP_LOG_INTERVAL:
                self._last_drop_log = now
                log.error("telemetry queue full; dropping records",
                          extra={"data": {"id": record.id,
                                          "dropped_total": self.dropped}})

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="telemetry-writer")
        log.info("telemetry writer started")

    async def stop(self) -> None:
        if self._task:
            await self.queue.put(_STOP)
            await self._task
            self._task = None
        log.info("telemetry writer stopped",
                 extra={"data": {"written": self.written}})

    async def flush(self) -> None:
        """Wait until everything queued so far is on disk (used by tests)."""
        await self.queue.join()

    async def _run(self) -> None:
        while True:
            item = await self.queue.get()
            if item is _STOP:
                self.queue.task_done()
                return
            batch = [item]
            while not self.queue.empty() and len(batch) < _BATCH_SIZE:
                nxt = self.queue.get_nowait()
                if nxt is _STOP:
                    await self._write(batch)
                    self.queue.task_done()
                    for _ in batch:
                        self.queue.task_done()
                    return
                batch.append(nxt)
            await self._write(batch)
            for _ in batch:
                self.queue.task_done()

    async def _write(self, batch: list[RequestRecord]) -> None:
        try:
            bodies = [
                (r.id, body_pack(r.prompt_body), body_pack(r.completion_body))
                for r in batch
                if r.prompt_body is not None or r.completion_body is not None
            ]
            sum_rows, hist_rows = self._roll_up(batch)
            # One transaction, one lock acquisition, one commit for the whole
            # batch — see Database.write_batch.
            await self.db.awrite_batch([
                (_INSERT, [_row(r) for r in batch]),
                (_INSERT_BODY, bodies),
                (SUM_UPSERT, sum_rows),
                (HIST_UPSERT, hist_rows),
            ])
            self.written += len(batch)
            if log.isEnabledFor(logging.DEBUG):
                for r in batch:
                    log.debug("recorded request", extra={"data": {
                        "id": r.id, "route": r.route, "model": r.model,
                        "endpoint": r.endpoint_name, "ok": r.ok,
                        "status": r.status, "tokens": r.total_tokens,
                        "ttft_ms": r.ttft_ms, "latency_ms": r.latency_ms,
                    }})
        except Exception:
            log.exception("telemetry write failed",
                          extra={"data": {"batch": len(batch)}})

    @staticmethod
    def _roll_up(batch: list[RequestRecord]) -> tuple[list, list]:
        """Fold this batch into hourly rollup rows (the same additive UPSERTs
        the backfill uses). Keeps dashboard queries O(buckets), not O(rows)."""
        items = [{
            "ts": r.ts, "endpoint_id": r.endpoint_id, "model": r.model,
            "ok": r.ok, "prompt_tokens": r.prompt_tokens,
            "completion_tokens": r.completion_tokens,
            "total_tokens": r.total_tokens, "cost_usd": r.cost_usd,
            "ttft_ms": r.ttft_ms, "latency_ms": r.latency_ms,
            "tokens_per_sec": r.tokens_per_sec,
            "endpoint_name": r.endpoint_name,
        } for r in batch]
        return build_rollup_rows(items)

    async def prune(self, retention_days: int) -> int:
        """Delete raw rows past retention. Called periodically from the app.

        ``retention_days <= 0`` means "keep forever": nothing is deleted. The
        hourly rollup tables are never pruned regardless, so aggregate history
        (dashboard charts) survives even when a finite raw retention is set.

        Done in bounded chunks, with a yield between them. There is one write
        connection behind one lock, so a single statement that runs for
        seconds stalls every telemetry write for that long — and the old
        orphan sweep (``id NOT IN (SELECT id FROM requests)``) was an
        unindexed anti-join across the whole bodies table, once an hour,
        holding that lock throughout. Chunking trades a slightly longer prune
        for never blocking the hot path.
        """
        if retention_days <= 0:
            self._last_prune = time.time()
            return 0
        cutoff = time.time() - retention_days * 86400
        n = 0
        while True:
            doomed = await self.db.aquery(
                "SELECT id FROM requests WHERE ts < ? LIMIT ?",
                (cutoff, _PRUNE_CHUNK))
            if not doomed:
                break
            ids = [r["id"] for r in doomed]
            marks = ",".join("?" * len(ids))
            # Bodies first: an interrupted prune then leaves rows whose body
            # is gone, never bodies no query can reach.
            await self.db.aexecute(
                f"DELETE FROM request_bodies WHERE id IN ({marks})", ids)
            await self.db.aexecute(
                f"DELETE FROM requests WHERE id IN ({marks})", ids)
            n += len(ids)
            await asyncio.sleep(0)  # let queued writes through
        while True:
            removed = await self.db.aexecute(
                "DELETE FROM frontend_logs WHERE id IN"
                " (SELECT id FROM frontend_logs WHERE ts < ? LIMIT ?)",
                (cutoff, _PRUNE_CHUNK))
            if not removed:
                break
            await asyncio.sleep(0)
        if n:
            log.info("pruned telemetry", extra={"data": {
                "rows": n, "retention_days": retention_days}})
        self._last_prune = time.time()
        return n


# How long a client key stays counted as "active", and a hard ceiling so a
# flood of distinct keys can't grow the registry without bound.
_CLIENT_TTL = 3600.0
_MAX_TRACKED_CLIENTS = 10_000


class LiveTracker:
    """In-flight request registry + rolling concurrency series."""

    def __init__(self, max_points: int = 240):
        self.in_flight: dict[str, dict] = {}
        self.series: deque[list] = deque(maxlen=max_points)
        self.requests_total = 0
        self.clients: dict[str, float] = {}  # masked key -> last seen ts

    def start(self, req_id: str, info: dict) -> None:
        self.in_flight[req_id] = {
            **info, "id": req_id, "state": "streaming",
            "completion_tokens": info.get("completion_tokens", 0),
        }
        self.requests_total += 1
        key = info.get("client_key")
        if key:
            self.clients[key] = time.time()
        self._sample()

    def update(self, req_id: str, info: dict) -> None:
        """Fill in fields that are only known after the request is under way
        (e.g. the resolved endpoint once failover/aliasing has picked one)."""
        row = self.in_flight.get(req_id)
        if row is not None:
            row.update(info)

    def bump(self, req_id: str, completion_tokens: int) -> None:
        row = self.in_flight.get(req_id)
        if row is not None:
            row["completion_tokens"] = completion_tokens

    def set_ttft(self, req_id: str, ttft_ms: float) -> None:
        row = self.in_flight.get(req_id)
        if row is not None:
            row["ttft_ms"] = ttft_ms

    def finish(self, req_id: str) -> None:
        self.in_flight.pop(req_id, None)
        self._sample()

    def _sample(self) -> None:
        now_ms = time.time() * 1000
        n = len(self.in_flight)
        if self.series and self.series[-1][0] >= now_ms:
            self.series[-1][1] = n
        else:
            self.series.append([now_ms, n])

    def sample(self) -> None:
        """Periodic sampler tick (keeps the series moving when idle)."""
        self._sample()
        self._expire_clients()

    def _expire_clients(self) -> None:
        """Drop client keys nobody has used inside the reporting window.

        ``clients`` is keyed by masked bearer token, so a relay open to the
        internet grows one entry per distinct key seen, forever — and
        ``active_clients`` walks all of them on every dashboard poll.
        """
        cutoff = time.time() - _CLIENT_TTL
        if len(self.clients) > _MAX_TRACKED_CLIENTS or any(
                ts <= cutoff for ts in self.clients.values()):
            self.clients = {k: ts for k, ts in self.clients.items()
                            if ts > cutoff}
        if len(self.clients) > _MAX_TRACKED_CLIENTS:
            # Still too many inside the window: keep the most recent.
            newest = sorted(self.clients.items(), key=lambda kv: -kv[1])
            self.clients = dict(newest[:_MAX_TRACKED_CLIENTS])

    def active_clients(self, window_s: float = 3600) -> int:
        cutoff = time.time() - window_s
        return sum(1 for ts in self.clients.values() if ts > cutoff)

    def snapshot(self, max_concurrency: int) -> dict:
        return {
            "in_flight": len(self.in_flight),
            "max_concurrency": max_concurrency,
            "series": [list(p) for p in self.series],
        }
