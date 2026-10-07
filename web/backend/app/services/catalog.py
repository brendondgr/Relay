"""The synthesized ``GET /v1/models``.

relay never forwards ``/v1/models``: the upstreams' own catalogs name models
relay can't necessarily route, and they don't know relay's aliases. The list
here is "auto", every enabled endpoint alias, and every model an endpoint
declares in its allowlist — so an OpenAI client discovers both the routing
names (skynet, local, ...) and the concrete models it can ask for by name
(anthropic/claude-sonnet-4-5, ...). Everything listed is routable: sending an
id back as "model" reaches the endpoint that advertised it.

Each entry carries ``meta`` (context window, tools, vision, reasoning; see
``model_facts.py``). "auto" gets the facts of the endpoint it resolves to
right now, so a client that only ever says "auto" still learns its window.
"""

import time

from app.services.model_facts import empty_meta


def _entry(model_id: str, created: int, row: dict, health: str,
           upstream: str | None) -> dict:
    return {
        "id": model_id, "object": "model", "created": created,
        "owned_by": f"relay:{row['name']}",
        "relay": {
            "endpoint": row["name"], "health": health,
            "protocol": row.get("protocol") or "openai",
            "upstream_model": upstream,
        },
    }


async def models_catalog(router, auto_failover: bool, facts=None) -> dict:
    """The ``/v1/models`` body. ``facts`` is a ``ModelFacts``; without one
    every ``meta`` is unknown."""
    created = int(time.time())
    data = [{"id": "auto", "object": "model", "created": created,
             "owned_by": "relay",
             "relay": {"routing": "active endpoint (hot-swap)"}}]
    auto_row = router.resolve(auto_failover=auto_failover)
    pairs: list[tuple[dict | None, str | None]] = [
        (auto_row,
         router.upstream_model(auto_row["id"]) if auto_row else None)]
    for row in router.endpoints.values():
        if not row["enabled"]:
            continue
        st = router.state.get(row["id"])
        health = st.health if st else "unknown"
        if row.get("alias"):
            upstream = router.upstream_model(row["id"])
            data.append(_entry(row["alias"], created, row, health, upstream))
            pairs.append((row, upstream))
        for model in router.available_models(row["id"]):
            data.append(_entry(model, created, row, health, model))
            pairs.append((row, model))

    known = [(row, model) for row, model in pairs if row is not None]
    found = iter(await facts.lookup(known) if facts else [])
    for entry, (row, _) in zip(data, pairs):
        entry["meta"] = (next(found, empty_meta()) if row is not None
                         else empty_meta())
    return {"object": "list", "data": data}
