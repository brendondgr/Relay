"""Routes for GAIFA (its architecture §4.6 and brief §11.7): the status GAIFA polls for the
Overview tile, and the "LLM traffic" dashboard widget. Both work without GAIFA too."""

import time

from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter(tags=["gaifa"])
WIDGET_RANGES = ("24h", "7d")


def _midnight(now: float | None = None) -> float:
    lt = time.localtime(now if now is not None else time.time())
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


@router.get("/admin/status")
async def status(request: Request):
    """{loaded: null, busy, stats}: requests and tokens today, p50 TTFT over 24 h, healthy endpoints."""
    stats, rt = request.app.state.stats, request.app.state.router
    today = await stats.summary(window=None, from_ts=_midnight())
    day = await stats.summary(window="24h")
    rows = [r for r in rt.endpoints.values() if r["enabled"]]
    healthy = sum(1 for r in rows if rt.state[r["id"]].health == "healthy")
    live = request.app.state.live.snapshot(request.app.state.proxy.gate.max_concurrency)
    return {
        "loaded": None,
        "busy": bool(live.get("in_flight")),
        "stats": [
            {"label": "Requests today", "value": today["requests"], "reset": "daily"},
            {"label": "Tokens today", "value": today["total_tokens"] or 0, "reset": "daily"},
            # No requests in 24 h means no p50; a stat value can't be null (architecture §4.6).
            {"label": "p50 TTFT", "value": day["ttft_ms"]["p50"], "unit": "ms"} if day["ttft_ms"]["p50"] is not None
            else {"label": "p50 TTFT", "value": "–", "hint": "no requests in 24 h"},
            {"label": "Endpoints", "value": f"{healthy}/{len(rows)}", "hint": "healthy"},
        ],
    }


def _ts(label: str) -> float:
    """A stats bucket label (local time: "YYYY-MM-DDTHH:MM", "YYYY-MM-DD" or "YYYY-MM") -> unix seconds."""
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d", "%Y-%m"):
        try:
            return time.mktime(time.strptime(label, fmt))
        except ValueError:
            continue
    raise ValueError(f"unknown bucket label {label!r}")


@router.get("/admin/hub/widgets/traffic")
async def traffic_widget(request: Request, range: str = Query("24h", description="24h or 7d")):
    """Requests and tokens per hour, plus p50 TTFT, as GAIFA WidgetData."""
    if range not in WIDGET_RANGES:
        raise HTTPException(422, f"range must be one of {list(WIDGET_RANGES)}")
    stats = request.app.state.stats
    # "detailed" keeps hourly-or-finer buckets for 7d too (the summary view is daily there).
    detail = "summary" if range == "24h" else "detailed"
    vol = await stats.volume(window=range, detail=detail)
    tok = await stats.tokens_timeseries(window=range, detail=detail)
    lat = await stats.latency(window=range)
    t = [_ts(row[0]) for row in vol["source"]]
    by = lambda ds, col: {r[0]: r[ds["dimensions"].index(col)] for r in ds["source"]}  # noqa: E731
    inp, out, ttft = by(tok, "input"), by(tok, "output"), by(lat, "ttft_p50")
    keys = [row[0] for row in vol["source"]]
    return {
        "unit": "count", "t": t,
        "series": [
            {"name": "Requests", "values": [row[1] for row in vol["source"]]},
            {"name": "Tokens", "values": [(inp.get(k) or 0) + (out.get(k) or 0) for k in keys], "axis": "right"},
            {"name": "p50 TTFT (ms)", "values": [ttft.get(k) or None for k in keys], "axis": "right"},
        ],
    }
