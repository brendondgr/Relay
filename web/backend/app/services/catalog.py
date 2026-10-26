"""The synthesized ``GET /v1/models``.

relay never forwards ``/v1/models``: the upstreams' own catalogs name models
relay can't necessarily route, and they don't know relay's aliases. The list
here is "auto", every enabled endpoint alias, every model an endpoint
declares in its allowlist, and the models a listed endpoint serves as
``<alias>/<model>`` (services/discovery.py) — so an OpenAI client discovers both the routing
names (skynet, local, ...) and the concrete models it can ask for by name
(anthropic/claude-sonnet-4-5, ...). Everything listed is routable: sending an
id back as "model" reaches the endpoint that advertised it.

Each entry carries ``meta`` (context window, tools, vision, reasoning; see
``model_facts.py``). "auto" gets the facts of the endpoint it resolves to
right now, so a client that only ever says "auto" still learns its window.
"""

import time

from app.services.discovery import listed_models
from app.services.model_facts import empty_meta, parse_override


def group(row: dict) -> str:
    """Where a server runs, for a picker's sections: local, remote, cloud, or agent (OpenCode)."""
    if (row.get("protocol") or "openai") == "opencode":
        return "agent"
    kind = row.get("kind")
    return "cloud" if kind == "cloud" else "local" if kind == "local" else "remote"


def _entry(model_id: str, created: int, row: dict, health: str,
           upstream: str | None) -> dict:
    return {
        "id": model_id, "object": "model", "created": created,
        "owned_by": f"relay:{row['name']}",
        "relay": {
            "endpoint": row["name"], "health": health,
            "protocol": row.get("protocol") or "openai",
            "upstream_model": upstream,
            "alias": row.get("alias"), "group": group(row),
        },
    }


async def models_catalog(router, auto_failover: bool, facts=None, discovery=None) -> dict:
    """The ``/v1/models`` body. ``facts`` is a ``ModelFacts``; without one
    every ``meta`` is unknown. With ``discovery``, each listed endpoint's own
    models follow as ``<alias>/<model>`` (GAIFA plan llm-servers 1.3), their
    facts from what the provider reported (an endpoint's override wins)."""
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

    # An alias owns its `<alias>/…` namespace: another endpoint's allowlisted id
    # under it (OpenCode's `openai/gpt-x` beside an endpoint aliased `openai`)
    # would never be reached, so it isn't offered.
    aliases = {(r.get("alias") or "").lower(): r["id"] for r in router.endpoints.values() if r.get("alias")}

    def shadowed(entry: dict, row: dict | None) -> bool:
        prefix, sep, _ = entry["id"].partition("/")
        owner = aliases.get(prefix.lower()) if sep else None
        return owner is not None and row is not None and owner != row["id"]
    kept = [(e, p) for e, p in zip(data, pairs) if not shadowed(e, p[0])]
    data, pairs = [e for e, _ in kept], [p for _, p in kept]

    known = [(row, model) for row, model in pairs if row is not None]
    found = iter(await facts.lookup(known) if facts else [])
    for entry, (row, _) in zip(data, pairs):
        entry["meta"] = (next(found, empty_meta()) if row is not None
                         else empty_meta())

    if discovery is not None:
        for row in list(router.endpoints.values()):
            if not row["enabled"] or not row.get("alias") or not listed_models(row):
                continue
            st = router.state.get(row["id"])
            health = st.health if st else "unknown"
            override = parse_override(row.get("facts_override"))
            for m in await discovery.visible(row["id"]):
                e = _entry(f"{row['alias']}/{m.id}", created, row, health, m.id)
                e["relay"]["discovered"] = True
                e["relay"]["display_name"] = m.display_name
                meta = {**m.facts(), "source": "server"}
                meta.update({k: v for k, v in override.items() if v is not None})
                e["meta"] = meta
                data.append(e)
    return {"object": "list", "data": data}
