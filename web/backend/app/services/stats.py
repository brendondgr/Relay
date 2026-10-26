"""Aggregation queries shaped for ECharts ``dataset`` binding.

Every timeseries endpoint returns ``{dimensions: [...], source: [[...], ...]}``
so the frontend does zero reshaping. Buckets are local-time (the dashboard's
hour-of-day and per-day charts are local-time concepts), zero-filled across
the window so category axes stay dense.

Scale strategy: hour-aligned windows (24h/7d/30d/all/custom) read from the
pre-aggregated ``request_rollup_hourly`` / ``request_rollup_hist`` tables, so
query cost is O(hours) instead of O(requests) and stays flat as the raw table
grows into the millions. The 1h window (minute buckets) and short-window
percentiles still read raw rows, which are bounded and cheap. Percentiles are
therefore *hybrid*: exact from raw for spans <= 24h, approximate from summed
histograms for wider spans (see ``services/histogram.py``).
"""

import time
from math import ceil, floor

from app.db import Database
from app.services.histogram import percentile_from_hist
from app.services.telemetry import LiveTracker

_WINDOWS = {"1h": 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400,
            "1y": 365 * 86400}
_MINUTE_FMT = "%Y-%m-%dT%H:%M"
_HOUR_FMT = "%Y-%m-%dT%H:00"
_DAY_FMT = "%Y-%m-%d"

# Time-series tick granularity for the combined volume/tokens chart, keyed by
# (window, detail). "summary" gives coarse at-a-glance buckets; "detailed"
# subdivides each window. Units coarser than an hour aggregate the hourly
# rollups; finer units (minute, 15min) scan raw rows.
_TS_BUCKETS = {
    ("1h", "summary"): "minute",   ("1h", "detailed"): "minute",
    ("24h", "summary"): "hour",    ("24h", "detailed"): "15min",
    ("7d", "summary"): "day",      ("7d", "detailed"): "hour",
    ("30d", "summary"): "day",     ("30d", "detailed"): "3hour",
    ("1y", "summary"): "month",    ("1y", "detailed"): "day",
}
_UNIT_STEP = {"minute": 60, "15min": 900, "hour": 3600,
              "3hour": 10800, "day": 86400}
_ROLLUP_UNITS = {"hour", "3hour", "day", "month"}

# Percentiles are computed exactly (from raw rows) for spans up to this width,
# and approximately (from histograms) beyond it. 24h + a minute of slack.
_EXACT_PCT_SPAN = 86400 + 60

# ...but "exact" still has to be bounded. At a few hundred requests a second a
# 24h window is millions of rows, and materializing them as dicts to sort in
# Python is seconds of CPU *per dashboard poll* — with several screens polling,
# that is the single most expensive thing relay does under load. Past this many
# rows the scan is sampled by rowid stride, which SQLite evaluates straight off
# the ts index. A uniform sample of 20k moves a p95 by well under a percent and
# keeps the query flat as the table grows.
_MAX_PCT_SAMPLES = 20_000


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    k = (len(vals) - 1) * pct / 100
    lo, hi = floor(k), ceil(k)
    if lo == hi:
        return round(vals[lo], 1)
    return round(vals[lo] + (vals[hi] - vals[lo]) * (k - lo), 1)


class StatsService:
    def __init__(self, db: Database, live: LiveTracker):
        self.db = db
        self.live = live

    # ---- window helpers ---------------------------------------------------
    async def _range(self, window: str | None, from_ts: float | None,
                     to_ts: float | None) -> tuple[float, float, int, str]:
        """-> (start, end, bucket_seconds, strftime_fmt)

        Async because the "all" window has to ask the DB where history
        starts, and a synchronous sqlite call here blocked the event loop for
        every other request in flight.
        """
        now = time.time()
        if from_ts is not None:
            start = from_ts
            end = to_ts or now
            return start, end, 3600, _HOUR_FMT
        w = window or "24h"
        if w == "all":
            row = await self.db.aquery_one("SELECT MIN(ts) AS t FROM requests")
            start = (row["t"] if row and row["t"] else now - 86400)
            return start, now, 3600, _HOUR_FMT
        span = _WINDOWS.get(w, 86400)
        bucket = 60 if w == "1h" else 3600
        fmt = _MINUTE_FMT if w == "1h" else _HOUR_FMT
        return now - span, now, bucket, fmt

    @staticmethod
    def _filters(endpoint_id: str | None, model: str | None,
                 start: float | None = None,
                 end: float | None = None) -> tuple[str, list]:
        clauses, params = [], []
        if start is not None:
            clauses.append("ts >= ?")
            params.append(start)
        if end is not None:
            clauses.append("ts <= ?")
            params.append(end)
        if endpoint_id:
            clauses.append("endpoint_id = ?")
            params.append(endpoint_id)
        if model:
            clauses.append("model = ?")
            params.append(model)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    @staticmethod
    def _roll_where(endpoint_id: str | None, model: str | None,
                    start: float, end: float) -> tuple[str, list]:
        """WHERE clause against the rollup tables (bucket_hour range + filters).
        NULL endpoint/model are stored as '' in rollups, so filters match."""
        clauses = ["bucket_hour >= ?", "bucket_hour <= ?"]
        params: list = [int(start // 3600) * 3600, end]
        if endpoint_id:
            clauses.append("endpoint_id = ?")
            params.append(endpoint_id)
        if model:
            clauses.append("model = ?")
            params.append(model)
        return " WHERE " + " AND ".join(clauses), params

    async def _sample_stride(self, where: str, params: list) -> int:
        """Rowid stride that keeps a raw percentile scan near
        ``_MAX_PCT_SAMPLES`` rows. 1 means "read them all"."""
        row = await self.db.aquery_one(
            f"SELECT COUNT(*) AS n FROM requests{where}", params)
        n = (row or {}).get("n") or 0
        if n <= _MAX_PCT_SAMPLES:
            return 1
        return ceil(n / _MAX_PCT_SAMPLES)

    @staticmethod
    def _sampled(where: str, stride: int) -> str:
        """Add the stride filter to a WHERE clause. ``rowid`` lives in the ts
        index entry, so this narrows the scan without extra row lookups."""
        if stride <= 1:
            return where
        clause = f"rowid % {stride} = 0"
        return f"{where} AND {clause}" if where else f" WHERE {clause}"

    @staticmethod
    def _use_rollup(step: int) -> bool:
        """Rollups are hourly; only the minute-bucketed 1h window needs raw."""
        return step == 3600

    @staticmethod
    def _fill(start: float, end: float, step: int, fmt: str) -> list[str]:
        keys = []
        t = floor(start / step) * step
        while t <= end:
            keys.append(time.strftime(fmt, time.localtime(t)))
            t += step
        return keys

    # ---- combined-chart timeseries bucketing ------------------------------
    async def _ts_range(self, window: str | None, detail: str | None,
                        from_ts: float | None,
                        to_ts: float | None) -> tuple[float, float, str]:
        """-> (start, end, unit) for the volume / tokens timeseries. Async for
        the same reason as :meth:`_range`."""
        now = time.time()
        if from_ts is not None:
            return from_ts, (to_ts or now), "hour"
        w = window or "24h"
        if w == "all":
            row = await self.db.aquery_one("SELECT MIN(ts) AS t FROM requests")
            start = row["t"] if row and row["t"] else now - 86400
            return start, now, "day"
        d = detail if detail in ("summary", "detailed") else "summary"
        unit = _TS_BUCKETS.get((w, d), "hour")
        return now - _WINDOWS.get(w, 86400), now, unit

    @staticmethod
    def _bucket_key(ts: float, unit: str) -> str:
        """Canonical local-time bucket label for ``ts`` at ``unit``. Matches
        the SQL expression in :meth:`_raw_bucket_expr` exactly."""
        lt = time.localtime(ts)
        if unit == "minute":
            return time.strftime("%Y-%m-%dT%H:%M", lt)
        if unit == "15min":
            return (time.strftime("%Y-%m-%dT%H:", lt)
                    + f"{(lt.tm_min // 15) * 15:02d}")
        if unit == "hour":
            return time.strftime("%Y-%m-%dT%H:00", lt)
        if unit == "3hour":
            return (time.strftime("%Y-%m-%dT", lt)
                    + f"{(lt.tm_hour // 3) * 3:02d}:00")
        if unit == "day":
            return time.strftime("%Y-%m-%d", lt)
        if unit == "month":
            return time.strftime("%Y-%m", lt)
        raise ValueError(f"unknown bucket unit: {unit}")

    @staticmethod
    def _raw_bucket_expr(unit: str) -> str:
        """SQLite expression that emits the same key as :meth:`_bucket_key`."""
        lt = "ts, 'unixepoch', 'localtime'"
        if unit == "minute":
            return f"strftime('%Y-%m-%dT%H:%M', {lt})"
        if unit == "15min":
            return (f"strftime('%Y-%m-%dT%H:', {lt}) || printf('%02d',"
                    f" (CAST(strftime('%M', {lt}) AS INTEGER) / 15) * 15)")
        if unit == "hour":
            return f"strftime('%Y-%m-%dT%H:00', {lt})"
        if unit == "day":
            return f"strftime('%Y-%m-%d', {lt})"
        raise ValueError(f"unit {unit} has no raw expression")

    @staticmethod
    def _fill_unit(start: float, end: float, unit: str) -> list[str]:
        """Dense, ordered bucket keys spanning [start, end] at ``unit``."""
        if unit == "month":
            s, e = time.localtime(start), time.localtime(end)
            keys, y, m = [], s.tm_year, s.tm_mon
            while (y, m) <= (e.tm_year, e.tm_mon):
                keys.append(f"{y:04d}-{m:02d}")
                m += 1
                if m > 12:
                    m, y = 1, y + 1
            return keys
        step = _UNIT_STEP[unit]
        keys: list[str] = []
        t = floor(start / step) * step
        while t <= end:
            key = StatsService._bucket_key(t, unit)
            if not keys or keys[-1] != key:
                keys.append(key)
            t += step
        return keys

    async def _ts_agg(self, unit: str, start: float, end: float,
                      endpoint_id: str | None, model: str | None,
                      roll_select: str, raw_select: str,
                      n_values: int) -> dict[str, list]:
        """Zero-filled timeseries summed into ``unit`` buckets. Rollup source
        for hour-and-coarser units, raw rows for sub-hour units."""
        acc = {k: [0] * n_values for k in self._fill_unit(start, end, unit)}
        if unit in _ROLLUP_UNITS:
            where, params = self._roll_where(endpoint_id, model, start, end)
            rows = await self.db.aquery(
                f"SELECT bucket_hour, {roll_select} FROM request_rollup_hourly"
                f"{where} GROUP BY bucket_hour", params)
            for r in rows:
                key = self._bucket_key(r["bucket_hour"], unit)
                if key in acc:
                    for i in range(n_values):
                        acc[key][i] += r[f"v{i}"] or 0
        else:
            where, params = self._filters(endpoint_id, model, start, end)
            expr = self._raw_bucket_expr(unit)
            rows = await self.db.aquery(
                f"SELECT {expr} AS bucket, {raw_select} FROM requests"
                f"{where} GROUP BY bucket", params)
            for r in rows:
                key = r["bucket"]
                if key in acc:
                    for i in range(n_values):
                        acc[key][i] += r[f"v{i}"] or 0
        return acc

    # ---- percentile sources ----------------------------------------------
    async def _hist_counts(self, metrics: tuple[str, ...], where: str,
                           params: list) -> dict[str, dict[int, int]]:
        """Sum histogram bucket counts over a range -> {metric: {idx: count}}."""
        placeholders = ",".join("?" for _ in metrics)
        rows = await self.db.aquery(
            "SELECT metric, bucket_idx, SUM(count) AS c FROM request_rollup_hist"
            f"{where} AND metric IN ({placeholders})"
            " GROUP BY metric, bucket_idx",
            [*params, *metrics])
        out: dict[str, dict[int, int]] = {m: {} for m in metrics}
        for r in rows:
            out[r["metric"]][r["bucket_idx"]] = r["c"]
        return out

    # ---- endpoints ---------------------------------------------------------
    async def summary(self, window: str | None, from_ts=None, to_ts=None,
                      endpoint_id=None, model=None) -> dict:
        start, end, step, _ = await self._range(window, from_ts, to_ts)
        exact = (end - start) <= _EXACT_PCT_SPAN

        if self._use_rollup(step):
            where, params = self._roll_where(endpoint_id, model, start, end)
            agg = await self.db.aquery_one(
                "SELECT COALESCE(SUM(n),0) AS requests,"
                " COALESCE(SUM(errors),0) AS errors,"
                " COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,"
                " COALESCE(SUM(completion_tokens),0) AS completion_tokens,"
                " COALESCE(SUM(total_tokens),0) AS total_tokens,"
                " COALESCE(SUM(cost_usd),0) AS cost_usd,"
                " COALESCE(SUM(tps_sum),0) AS tps_sum,"
                " COALESCE(SUM(tps_n),0) AS tps_n"
                f" FROM request_rollup_hourly{where}", params)
        else:
            rwhere, rparams = self._filters(endpoint_id, model, start, end)
            agg = await self.db.aquery_one(
                "SELECT COUNT(*) AS requests,"
                " SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS errors,"
                " COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,"
                " COALESCE(SUM(completion_tokens), 0) AS completion_tokens,"
                " COALESCE(SUM(total_tokens), 0) AS total_tokens,"
                " COALESCE(SUM(cost_usd), 0) AS cost_usd"
                f" FROM requests{rwhere}", rparams)

        if exact:
            rwhere, rparams = self._filters(endpoint_id, model, start, end)
            stride = await self._sample_stride(rwhere, rparams)
            samples = await self.db.aquery(
                "SELECT ttft_ms, latency_ms, tokens_per_sec"
                f" FROM requests{self._sampled(rwhere, stride)}", rparams)
            ttfts = [r["ttft_ms"] for r in samples if r["ttft_ms"]]
            lats = [r["latency_ms"] for r in samples if r["latency_ms"]]
            tpss = [r["tokens_per_sec"] for r in samples if r["tokens_per_sec"]]
            ttft = {"p50": _percentile(ttfts, 50), "p95": _percentile(ttfts, 95)}
            lat = {"p50": _percentile(lats, 50), "p95": _percentile(lats, 95)}
            tps = {"p50": _percentile(tpss, 50)}
        else:
            where, params = self._roll_where(endpoint_id, model, start, end)
            counts = await self._hist_counts(
                ("ttft", "latency", "tps"), where, params)
            ttft = {"p50": percentile_from_hist("ttft", counts["ttft"], 50),
                    "p95": percentile_from_hist("ttft", counts["ttft"], 95)}
            lat = {"p50": percentile_from_hist("latency", counts["latency"], 50),
                   "p95": percentile_from_hist("latency", counts["latency"], 95)}
            tps = {"p50": percentile_from_hist("tps", counts["tps"], 50)}

        requests = agg["requests"] or 0
        errors = agg["errors"] or 0
        return {
            "requests": requests,
            "errors": errors,
            "error_rate": round(errors / requests, 4) if requests else 0.0,
            "prompt_tokens": agg["prompt_tokens"],
            "completion_tokens": agg["completion_tokens"],
            "total_tokens": agg["total_tokens"],
            "cost_usd": round(agg["cost_usd"] or 0, 4),
            "ttft_ms": ttft,
            "latency_ms": lat,
            "tokens_per_sec": tps,
        }

    async def volume(self, window, from_ts=None, to_ts=None,
                     endpoint_id=None, model=None, detail="summary") -> dict:
        start, end, unit = await self._ts_range(window, detail, from_ts, to_ts)
        buckets = await self._ts_agg(
            unit, start, end, endpoint_id, model,
            "SUM(n) AS v0, SUM(errors) AS v1",
            "COUNT(*) AS v0, SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS v1", 2)
        return {
            "dimensions": ["time", "requests", "errors"],
            "source": [[k, *v] for k, v in buckets.items()],
        }

    async def tokens_timeseries(self, window, from_ts=None, to_ts=None,
                                endpoint_id=None, model=None,
                                detail="summary") -> dict:
        start, end, unit = await self._ts_range(window, detail, from_ts, to_ts)
        buckets = await self._ts_agg(
            unit, start, end, endpoint_id, model,
            "SUM(prompt_tokens) AS v0, SUM(completion_tokens) AS v1",
            "COALESCE(SUM(prompt_tokens), 0) AS v0,"
            " COALESCE(SUM(completion_tokens), 0) AS v1", 2)
        return {
            "dimensions": ["time", "input", "output"],
            "source": [[k, *v] for k, v in buckets.items()],
        }

    async def tokens_by_hour(self, window, from_ts=None, to_ts=None,
                             endpoint_id=None, model=None) -> dict:
        start, end, step, _ = await self._range(window or "30d", from_ts, to_ts)
        if self._use_rollup(step):
            where, params = self._roll_where(endpoint_id, model, start, end)
            rows = await self.db.aquery(
                "SELECT CAST(strftime('%H', bucket_hour, 'unixepoch',"
                " 'localtime') AS INTEGER) AS hour,"
                " SUM(prompt_tokens) AS tin, SUM(completion_tokens) AS tout"
                f" FROM request_rollup_hourly{where} GROUP BY hour", params)
        else:
            where, params = self._filters(endpoint_id, model, start, end)
            rows = await self.db.aquery(
                "SELECT CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS"
                " INTEGER) AS hour, COALESCE(SUM(prompt_tokens), 0) AS tin,"
                " COALESCE(SUM(completion_tokens), 0) AS tout"
                f" FROM requests{where} GROUP BY hour", params)
        by_hour = {r["hour"]: r for r in rows}
        source = [
            [h, (by_hour.get(h) or {}).get("tin", 0) or 0,
             (by_hour.get(h) or {}).get("tout", 0) or 0]
            for h in range(24)
        ]
        return {"dimensions": ["hour", "input", "output"], "source": source}

    async def tokens_by_day(self, window, from_ts=None, to_ts=None,
                            endpoint_id=None, model=None) -> dict:
        start, end, step, _ = await self._range(window or "7d", from_ts, to_ts)
        if self._use_rollup(step):
            where, params = self._roll_where(endpoint_id, model, start, end)
            rows = await self.db.aquery(
                f"SELECT strftime('{_DAY_FMT}', bucket_hour, 'unixepoch',"
                " 'localtime') AS day, SUM(prompt_tokens) AS tin,"
                " SUM(completion_tokens) AS tout"
                f" FROM request_rollup_hourly{where} GROUP BY day"
                " ORDER BY day", params)
        else:
            where, params = self._filters(endpoint_id, model, start, end)
            rows = await self.db.aquery(
                f"SELECT strftime('{_DAY_FMT}', ts, 'unixepoch', 'localtime')"
                " AS day, COALESCE(SUM(prompt_tokens), 0) AS tin,"
                " COALESCE(SUM(completion_tokens), 0) AS tout"
                f" FROM requests{where} GROUP BY day ORDER BY day", params)
        by_day = {r["day"]: r for r in rows}
        source = [
            [d, (by_day.get(d) or {}).get("tin", 0) or 0,
             (by_day.get(d) or {}).get("tout", 0) or 0]
            for d in self._fill(start, end, 86400, _DAY_FMT)
        ]
        return {"dimensions": ["date", "input", "output"], "source": source}

    async def by_model(self, window, from_ts=None, to_ts=None,
                       endpoint_id=None, model=None) -> dict:
        start, end, step, _ = await self._range(window, from_ts, to_ts)
        if self._use_rollup(step):
            where, params = self._roll_where(endpoint_id, model, start, end)
            rows = await self.db.aquery(
                "SELECT CASE WHEN model = '' THEN '—' ELSE model END AS model,"
                " SUM(n) AS requests, SUM(total_tokens) AS tokens,"
                " SUM(cost_usd) AS cost, SUM(tps_sum) AS tps_sum,"
                " SUM(tps_n) AS tps_n, SUM(errors) AS errors"
                f" FROM request_rollup_hourly{where} GROUP BY model"
                " ORDER BY requests DESC", params)
            rows = [
                {**r, "avg_tps": round(r["tps_sum"] / r["tps_n"], 1)
                 if r["tps_n"] else 0}
                for r in rows]
        else:
            where, params = self._filters(endpoint_id, model, start, end)
            rows = await self.db.aquery(
                "SELECT COALESCE(model, '—') AS model, COUNT(*) AS requests,"
                " COALESCE(SUM(total_tokens), 0) AS tokens,"
                " COALESCE(SUM(cost_usd), 0) AS cost,"
                " ROUND(AVG(tokens_per_sec), 1) AS avg_tps,"
                " SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS errors"
                f" FROM requests{where} GROUP BY model ORDER BY requests DESC",
                params)
        return {
            "dimensions": ["model", "requests", "tokens", "cost",
                           "avg_tps", "errors"],
            "source": [[r["model"], r["requests"], r["tokens"],
                        round(r["cost"] or 0, 4), r["avg_tps"] or 0,
                        r["errors"]] for r in rows],
        }

    async def by_endpoint(self, window, from_ts=None, to_ts=None,
                          endpoint_id=None, model=None) -> dict:
        start, end, step, _ = await self._range(window, from_ts, to_ts)
        if self._use_rollup(step):
            where, params = self._roll_where(endpoint_id, model, start, end)
            rows = await self.db.aquery(
                "SELECT COALESCE(MAX(endpoint_name), '—') AS endpoint,"
                " CASE WHEN endpoint_id = '' THEN NULL ELSE endpoint_id END"
                " AS endpoint_id, SUM(n) AS requests,"
                " SUM(prompt_tokens) AS input, SUM(completion_tokens) AS output,"
                " SUM(errors) AS errors"
                f" FROM request_rollup_hourly{where} GROUP BY endpoint_id"
                " ORDER BY requests DESC", params)
        else:
            where, params = self._filters(endpoint_id, model, start, end)
            rows = await self.db.aquery(
                "SELECT COALESCE(endpoint_name, '—') AS endpoint,"
                " endpoint_id, COUNT(*) AS requests,"
                " COALESCE(SUM(prompt_tokens), 0) AS input,"
                " COALESCE(SUM(completion_tokens), 0) AS output,"
                " SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS errors"
                f" FROM requests{where} GROUP BY endpoint_id"
                " ORDER BY requests DESC", params)
        total = sum(r["requests"] for r in rows) or 1
        return {
            "dimensions": ["endpoint", "requests", "input", "output",
                           "errors", "share", "endpoint_id"],
            "source": [[r["endpoint"], r["requests"], r["input"], r["output"],
                        r["errors"], round(r["requests"] / total, 4),
                        r["endpoint_id"]] for r in rows],
        }

    async def latency(self, window, from_ts=None, to_ts=None,
                      endpoint_id=None, model=None) -> dict:
        start, end, step, fmt = await self._range(window, from_ts, to_ts)
        keys = self._fill(start, end, step, fmt)
        if (end - start) <= _EXACT_PCT_SPAN:
            where, params = self._filters(endpoint_id, model, start, end)
            # Sampled uniformly across the window, so every bucket thins by
            # the same factor and the shape of the series is preserved.
            stride = await self._sample_stride(where, params)
            rows = await self.db.aquery(
                f"SELECT strftime('{fmt}', ts, 'unixepoch', 'localtime') AS"
                " bucket, ttft_ms, tokens_per_sec FROM requests"
                + self._sampled(where, stride), params)
            grouped: dict[str, dict[str, list]] = {}
            for r in rows:
                g = grouped.setdefault(r["bucket"], {"ttft": [], "tps": []})
                if r["ttft_ms"]:
                    g["ttft"].append(r["ttft_ms"])
                if r["tokens_per_sec"]:
                    g["tps"].append(r["tokens_per_sec"])
            source = []
            for key in keys:
                g = grouped.get(key, {"ttft": [], "tps": []})
                source.append([
                    key,
                    _percentile(g["ttft"], 50) or 0,
                    _percentile(g["ttft"], 95) or 0,
                    _percentile(g["tps"], 50) or 0,
                ])
        else:
            # Approximate per-hour percentiles from summed histograms.
            where, params = self._roll_where(endpoint_id, model, start, end)
            rows = await self.db.aquery(
                "SELECT bucket_hour, metric, bucket_idx, SUM(count) AS c"
                f" FROM request_rollup_hist{where}"
                " AND metric IN ('ttft','tps')"
                " GROUP BY bucket_hour, metric, bucket_idx", params)
            grouped: dict[str, dict[str, dict[int, int]]] = {}
            for r in rows:
                key = time.strftime(fmt, time.localtime(r["bucket_hour"]))
                g = grouped.setdefault(key, {"ttft": {}, "tps": {}})
                g[r["metric"]][r["bucket_idx"]] = r["c"]
            source = []
            for key in keys:
                g = grouped.get(key, {"ttft": {}, "tps": {}})
                source.append([
                    key,
                    percentile_from_hist("ttft", g["ttft"], 50) or 0,
                    percentile_from_hist("ttft", g["ttft"], 95) or 0,
                    percentile_from_hist("tps", g["tps"], 50) or 0,
                ])
        return {
            "dimensions": ["time", "ttft_p50", "ttft_p95", "tps_p50"],
            "source": source,
        }

    async def recent(self, limit: int = 90, group: str | None = None) -> dict:
        limit = min(limit, 500)
        if group:
            db_rows = await self.db.aquery(
                "SELECT * FROM requests WHERE grp = ? ORDER BY ts DESC LIMIT ?", (group, limit))
        else:
            db_rows = await self.db.aquery(
                "SELECT * FROM requests ORDER BY ts DESC LIMIT ?", (limit,))
        rows = []
        for info in sorted(self.live.in_flight.values(),
                           key=lambda r: -r.get("ts", 0)):
            rows.append({
                "id": info["id"], "ts": info.get("ts"),
                "endpoint_name": info.get("endpoint_name"),
                "route": info.get("route"), "model": info.get("model"),
                "stream": info.get("stream", True), "status": None,
                "ok": None, "state": "streaming", "error": None,
                "prompt_tokens": info.get("prompt_tokens"),
                "completion_tokens": info.get("completion_tokens", 0),
                "total_tokens": None,
                "ttft_ms": info.get("ttft_ms"), "latency_ms": None,
                "tokens_per_sec": None, "cost_usd": None,
                "temperature": info.get("temperature"),
                "max_tokens": info.get("max_tokens"),
            })
        for r in db_rows:
            rows.append({
                **{k: r.get(k) for k in (
                    "id", "ts", "endpoint_name", "route", "model", "status",
                    "error", "prompt_tokens", "completion_tokens",
                    "total_tokens", "ttft_ms", "latency_ms",
                    "tokens_per_sec", "cost_usd", "temperature",
                    "max_tokens", "grp", "cached_tokens", "cache_write_tokens")},
                "stream": bool(r.get("stream")),
                "ok": bool(r.get("ok")),
                "state": "done" if r.get("ok") else "error",
                "endpoint_id": r.get("endpoint_id"),
            })
        rows = rows[:limit]
        counts = {
            "all": len(rows),
            "streaming": sum(1 for r in rows if r["state"] == "streaming"),
            "done": sum(1 for r in rows if r["state"] == "done"),
            "error": sum(1 for r in rows if r["state"] == "error"),
        }
        return {"rows": rows, "counts": counts}
