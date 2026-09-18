"""SQLite storage. Zero-setup single node; schema written to stay
Postgres-compatible (TEXT ids, REAL unix-seconds timestamps, no SQLite-only
column types) so a future migration is a driver swap, not a redesign.

Concurrency model: WAL mode; one long-lived write connection guarded by a
lock (all mutations funnel through the telemetry writer or admin routes), and
one long-lived read-only connection *per reader thread* so dashboard polling
never contends with the hot path. Async callers use the ``a*`` wrappers.

Two details there are load-bearing under concurrency:

- **Readers are cached per thread, not opened per query.** Every ``query()``
  used to ``sqlite3.connect()`` and close again; at a few hundred requests a
  second that is an open/mmap/close of the WAL index per dashboard poll, and
  it showed up as latency on the hot path because it burned the same threads
  telemetry writes need.
- **The offload target is this module's own pool**, not ``asyncio.to_thread``.
  The default executor is shared with everything else in the process and is
  only ``min(32, cpu+4)`` wide, so a slow stats query could starve the
  telemetry writer — and vice versa.
"""

import asyncio
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
  id                TEXT PRIMARY KEY,
  ts                REAL NOT NULL,
  endpoint_id       TEXT,
  endpoint_name     TEXT,
  route             TEXT,
  model             TEXT,
  client_key        TEXT,
  stream            INTEGER,
  status            INTEGER,
  ok                INTEGER,
  error             TEXT,
  prompt_tokens     INTEGER,
  completion_tokens INTEGER,
  total_tokens      INTEGER,
  ttft_ms           REAL,
  latency_ms        REAL,
  tokens_per_sec    REAL,
  cost_usd          REAL,
  temperature       REAL,
  max_tokens        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_requests_ts    ON requests(ts);
CREATE INDEX IF NOT EXISTS idx_requests_model ON requests(model);
CREATE INDEX IF NOT EXISTS idx_requests_ep    ON requests(endpoint_id);

-- prompt/completion are zlib-compressed BLOBs (see telemetry.body_pack);
-- legacy rows may still hold plain TEXT and are handled by body_unpack.
CREATE TABLE IF NOT EXISTS request_bodies (
  id         TEXT PRIMARY KEY,
  prompt     BLOB,
  completion BLOB
);

CREATE TABLE IF NOT EXISTS endpoints (
  id             TEXT PRIMARY KEY,
  name           TEXT NOT NULL,
  alias          TEXT,
  kind           TEXT NOT NULL DEFAULT 'local',
  server_type    TEXT DEFAULT 'openai',
  -- Wire protocol the upstream speaks; the adapter dispatch key. Unlike
  -- `kind` (topology, re-derived on every PATCH) and `server_type`
  -- (cosmetic badge), nothing else writes this.
  protocol       TEXT NOT NULL DEFAULT 'openai',
  -- JSON array of model ids this endpoint is allowed to serve, or NULL for
  -- "whatever it advertises". Read through router.available_models.
  available_models TEXT,
  base_url       TEXT NOT NULL,
  upstream_key   TEXT,
  tunnel_id      TEXT,
  tunnel_command TEXT,
  tunnel_local_port INTEGER,
  priority       INTEGER NOT NULL DEFAULT 100,
  weight         INTEGER NOT NULL DEFAULT 1,
  enabled        INTEGER NOT NULL DEFAULT 1,
  model_override TEXT,
  created_ts     REAL
);

CREATE TABLE IF NOT EXISTS tunnel_routes (
  id            TEXT PRIMARY KEY,
  endpoint_id   TEXT NOT NULL,
  label         TEXT NOT NULL,
  command       TEXT NOT NULL,
  local_port    INTEGER,
  created_ts    REAL
);
CREATE INDEX IF NOT EXISTS idx_tunnel_routes_ep ON tunnel_routes(endpoint_id);

CREATE TABLE IF NOT EXISTS tunnels (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  ssh_host    TEXT NOT NULL,
  ssh_port    INTEGER NOT NULL DEFAULT 22,
  ssh_user    TEXT NOT NULL,
  key_path    TEXT,
  remote_host TEXT NOT NULL DEFAULT '127.0.0.1',
  remote_port INTEGER NOT NULL,
  local_port  INTEGER NOT NULL,
  compress    INTEGER NOT NULL DEFAULT 1,
  keepalive   INTEGER NOT NULL DEFAULT 1,
  extra_opts  TEXT,
  enabled     INTEGER NOT NULL DEFAULT 0,
  created_ts  REAL
);

-- Hourly pre-aggregation so the dashboard never scans the raw requests table.
-- endpoint_id/model use '' (not NULL) so the composite PK dedupes under UPSERT.
CREATE TABLE IF NOT EXISTS request_rollup_hourly (
  bucket_hour       INTEGER NOT NULL,
  endpoint_id       TEXT NOT NULL DEFAULT '',
  model             TEXT NOT NULL DEFAULT '',
  n                 INTEGER NOT NULL DEFAULT 0,
  errors            INTEGER NOT NULL DEFAULT 0,
  prompt_tokens     INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens      INTEGER NOT NULL DEFAULT 0,
  cost_usd          REAL    NOT NULL DEFAULT 0,
  tps_sum           REAL    NOT NULL DEFAULT 0,
  tps_n             INTEGER NOT NULL DEFAULT 0,
  endpoint_name     TEXT,
  PRIMARY KEY (bucket_hour, endpoint_id, model)
);
CREATE INDEX IF NOT EXISTS idx_rollup_hourly_h ON request_rollup_hourly(bucket_hour);

-- Additive per-hour latency/ttft/tps histograms for approximate percentiles on
-- wide windows (see services/histogram.py). Only non-zero buckets are stored.
CREATE TABLE IF NOT EXISTS request_rollup_hist (
  bucket_hour  INTEGER NOT NULL,
  endpoint_id  TEXT NOT NULL DEFAULT '',
  model        TEXT NOT NULL DEFAULT '',
  metric       TEXT NOT NULL,
  bucket_idx   INTEGER NOT NULL,
  count        INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (bucket_hour, endpoint_id, model, metric, bucket_idx)
);
CREATE INDEX IF NOT EXISTS idx_rollup_hist_h ON request_rollup_hist(bucket_hour);
-- Covering index for the wide-window percentile scan (GROUP BY metric,bucket_idx
-- with a bucket_hour range): lets SQLite aggregate index-only, no table lookup.
CREATE INDEX IF NOT EXISTS idx_rollup_hist_scan
  ON request_rollup_hist(metric, bucket_idx, bucket_hour, count);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS frontend_logs (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  ts     REAL NOT NULL,
  level  TEXT NOT NULL,
  event  TEXT NOT NULL,
  detail TEXT
);
"""


# Long enough to ride out a checkpoint or a batched write without surfacing
# "database is locked" to a caller, short enough to fail loudly if something
# is genuinely wedged.
_BUSY_TIMEOUT_MS = 10_000


class Database:
    def __init__(self, path: Path | str, threads: int = 8):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._closed = False
        # Reader connections, one per thread that ever reads, kept alive for
        # the life of the process and closed together in close().
        self._local = threading.local()
        self._readers: list[sqlite3.Connection] = []
        self._readers_lock = threading.Lock()
        self._pool = ThreadPoolExecutor(
            max_workers=max(2, threads), thread_name_prefix="relay-db")
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Additive migrations for DBs created before newer columns."""
        cols = {
            r[1] for r in self._conn.execute("PRAGMA table_info(endpoints)")
        }
        if "alias" not in cols:
            self._conn.execute("ALTER TABLE endpoints ADD COLUMN alias TEXT")
        if "model_override" not in cols:
            self._conn.execute(
                "ALTER TABLE endpoints ADD COLUMN model_override TEXT")
        if "tunnel_command" not in cols:
            self._conn.execute(
                "ALTER TABLE endpoints ADD COLUMN tunnel_command TEXT")
        if "tunnel_local_port" not in cols:
            self._conn.execute(
                "ALTER TABLE endpoints ADD COLUMN tunnel_local_port INTEGER")
        if "active_tunnel_route_id" not in cols:
            self._conn.execute(
                "ALTER TABLE endpoints ADD COLUMN active_tunnel_route_id TEXT")
        if "protocol" not in cols:
            self._conn.execute(
                "ALTER TABLE endpoints ADD COLUMN protocol TEXT NOT NULL"
                " DEFAULT 'openai'")
        if "available_models" not in cols:
            self._conn.execute(
                "ALTER TABLE endpoints ADD COLUMN available_models TEXT")
        # Registrations (services/registrations.py): endpoints another program
        # registers and removes by a stable key, e.g. lcpp's "lcpp:7071".
        # owner scopes cleanup to that program's own rows; meta is its JSON
        # blob (e.g. {"manager_url": ...}) shown by the dashboard.
        for col in ("owner", "external_key", "meta"):
            if col not in cols:
                self._conn.execute(f"ALTER TABLE endpoints ADD COLUMN {col} TEXT")
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_endpoints_external_key"
            " ON endpoints(external_key) WHERE external_key IS NOT NULL")
        # Backfill: an endpoint that already had a tunnel_command before
        # routes existed becomes its own "default" route, so upgrades don't
        # lose the working ssh command.
        orphans = self._conn.execute(
            "SELECT id, tunnel_command, tunnel_local_port FROM endpoints"
            " WHERE tunnel_command IS NOT NULL AND tunnel_command != ''"
            " AND (active_tunnel_route_id IS NULL OR active_tunnel_route_id = '')"
        ).fetchall()
        for eid, cmd, port in orphans:
            existing = self._conn.execute(
                "SELECT id FROM tunnel_routes WHERE endpoint_id = ? LIMIT 1",
                (eid,)).fetchone()
            if existing:
                rid = existing[0]
            else:
                rid = str(uuid.uuid4())
                self._conn.execute(
                    "INSERT INTO tunnel_routes (id, endpoint_id, label,"
                    " command, local_port, created_ts) VALUES (?,?,?,?,?,?)",
                    (rid, eid, "default", cmd, port, time.time()))
            self._conn.execute(
                "UPDATE endpoints SET active_tunnel_route_id = ? WHERE id = ?",
                (rid, eid))

        self._rollup_backfill()

    def _rollup_backfill(self) -> None:
        """Populate rollup tables from pre-existing raw rows exactly once.

        Runs during __init__ before the telemetry writer starts, so it can't
        race with live inserts (which maintain rollups incrementally from here
        on). Guarded by a settings watermark; idempotent across restarts.
        """
        # Local import avoids a db <-> telemetry/rollup import cycle.
        from app.services.rollup import (
            HIST_UPSERT, SUM_UPSERT, build_rollup_rows,
        )

        done = self._conn.execute(
            "SELECT value FROM settings WHERE key = 'rollup_built'"
        ).fetchone()
        if done and done[0] == "1":
            return

        cur = self._conn.execute(
            "SELECT ts, endpoint_id, model, ok, prompt_tokens,"
            " completion_tokens, total_tokens, cost_usd, ttft_ms, latency_ms,"
            " tokens_per_sec, endpoint_name FROM requests")
        cols = [d[0] for d in cur.description]
        total = 0
        while True:
            chunk = cur.fetchmany(5000)
            if not chunk:
                break
            items = [dict(zip(cols, row)) for row in chunk]
            sum_rows, hist_rows = build_rollup_rows(items)
            if sum_rows:
                self._conn.executemany(SUM_UPSERT, sum_rows)
            if hist_rows:
                self._conn.executemany(HIST_UPSERT, hist_rows)
            total += len(items)

        self._conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES"
            " ('rollup_built', '1')")
        # commit happens in __init__ after _migrate returns.

    # -- sync core -----------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur.rowcount

    def executemany(self, sql: str, rows: list[tuple]) -> int:
        if not rows:
            return 0
        with self._lock:
            cur = self._conn.executemany(sql, rows)
            self._conn.commit()
            return cur.rowcount

    def _reader(self) -> sqlite3.Connection:
        """This thread's read-only connection, opened once. WAL readers never
        block the writer, so every thread can hold one indefinitely."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            self._local.conn = conn
            with self._readers_lock:
                self._readers.append(conn)
        return conn

    def write_batch(self, statements: list[tuple[str, list[tuple]]]) -> int:
        """Apply several ``executemany`` statements in one transaction, under a
        single acquisition of the write lock.

        The telemetry writer's unit of work is four statements (rows, bodies,
        and the two rollup upserts). Issued separately that is four lock
        handoffs and four commits per batch, every one of them contending with
        the retention prune and with any admin write. As one transaction it is
        one of each, and a partial batch can no longer leave rollups
        disagreeing with the rows they summarize.
        """
        total = 0
        with self._lock:
            try:
                for sql, rows in statements:
                    if not rows:
                        continue
                    total += self._conn.executemany(sql, rows).rowcount
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return total

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        rows = self._reader().execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # -- async wrappers ------------------------------------------------
    async def _offload(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, fn, *args)

    async def aexecute(self, sql: str, params: Iterable[Any] = ()) -> int:
        return await self._offload(self.execute, sql, params)

    async def aexecutemany(self, sql: str, rows: list[tuple]) -> int:
        return await self._offload(self.executemany, sql, rows)

    async def awrite_batch(
        self, statements: list[tuple[str, list[tuple]]]
    ) -> int:
        return await self._offload(self.write_batch, statements)

    async def aquery(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        return await self._offload(self.query, sql, params)

    async def aquery_one(
        self, sql: str, params: Iterable[Any] = ()
    ) -> dict | None:
        return await self._offload(self.query_one, sql, params)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._pool.shutdown(wait=True)
        with self._readers_lock:
            readers, self._readers = self._readers, []
        for conn in readers:
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover - best-effort teardown
                pass
        with self._lock:
            self._conn.close()
