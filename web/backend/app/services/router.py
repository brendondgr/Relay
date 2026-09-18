"""Endpoint registry + live health state + resolution policy.

The pool lives in memory (rebuilt from DB on boot, mutated by the control
plane). Requests resolve their upstream at call time from this live state,
so hot-swap/failover is just a state mutation — no restart, in-flight
requests untouched.

Health state machine (active probes + passive request signals):

        probe ok / request ok
   ┌───────────────◀───────────────┐
HEALTHY ──fails ≥ N──▶ DEGRADED ──fails keep coming──▶ FAILED (out of pool)
   ▲                                 (only while probes                │
   │                                  are failing too)                 │
   └────────── M consecutive probe successes ◀────────────┘

Both edges into and out of FAILED are probe-gated, and for the same reason:
request outcomes say how the endpoint is *doing*, active probes say whether
it is *there*. A busy server times out requests without being gone.
"""

import json
import re
import time
import uuid
from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.db import Database
from app.schemas import EndpointCreate, EndpointOut, EndpointPatch, RouterState
from app.services.tunnel_sessions import parse_local_port, validate_command

log = get_logger("router")

_POLICY_KEY = "router_policy"
_PINNED_KEY = "router_pinned"
_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Model ids are upstream-owned names, so this is looser than the alias rule:
# it must admit "anthropic/claude-sonnet-4-5" and "qwen3:30b" as written.
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")
_RESERVED_ALIASES = {"auto"}


def parse_models(value) -> list[str]:
    """Read an ``available_models`` cell. Stored as a JSON array; tolerates
    NULL and (from a hand-edited DB) a plain comma-separated string."""
    if not value:
        return []
    if isinstance(value, list):
        return [str(m) for m in value]
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return [p.strip() for p in str(value).split(",") if p.strip()]
    if isinstance(parsed, list):
        return [str(m) for m in parsed]
    return []


def _meta(value) -> dict | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _models_json(models: list[str]) -> str | None:
    """Storage form: a JSON array, or NULL when the allowlist is empty."""
    return json.dumps(models) if models else None


@dataclass
class LiveState:
    health: str = "unknown"  # unknown|healthy|degraded|failed
    consecutive_fails: int = 0
    consecutive_probe_ok: int = 0
    # Outcome of the most recent active probe, independent of request
    # outcomes. None until one has run. See ``report_failure`` for why a
    # passing probe outranks a run of failed requests.
    last_probe_ok: bool | None = None
    ewma_latency_ms: float | None = None
    last_ok_ts: float | None = None
    model: str | None = None
    models: list[str] = field(default_factory=list)      # after the allowlist
    discovered: list[str] = field(default_factory=list)  # raw from the probe


def _infer_kind(url: str, tunnel_id: str | None,
                tunnel_command: str | None = None) -> str:
    if tunnel_id or (tunnel_command or "").strip():
        return "remote_tunnel"
    if "127.0.0.1" in url or "localhost" in url:
        return "local"
    return "remote_direct"


class Router:
    def __init__(self, db: Database, unhealthy_after: int = 3, recover_after: int = 2):
        self.db = db
        self.unhealthy_after = unhealthy_after
        self.recover_after = recover_after
        self.endpoints: dict[str, dict] = {}
        self.state: dict[str, LiveState] = {}
        self.policy: str = "manual"
        self.pinned_id: str | None = None
        self._load()

    # ---- persistence -----------------------------------------------------
    def _load(self) -> None:
        for row in self.db.query("SELECT * FROM endpoints"):
            self.endpoints[row["id"]] = row
            self.state[row["id"]] = LiveState()
        pol = self.db.query_one(
            "SELECT value FROM settings WHERE key = ?", (_POLICY_KEY,))
        pin = self.db.query_one(
            "SELECT value FROM settings WHERE key = ?", (_PINNED_KEY,))
        self.policy = pol["value"] if pol else "manual"
        self.pinned_id = pin["value"] if pin and pin["value"] in self.endpoints else None
        if self.pinned_id is None and self.endpoints:
            self.pinned_id = next(iter(self.endpoints))
        log.info("router loaded", extra={"data": {
            "endpoints": len(self.endpoints), "policy": self.policy,
            "pinned": self.pinned_id}})

    def _persist_router(self) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (_POLICY_KEY, self.policy))
        self.db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (_PINNED_KEY, self.pinned_id or ""))

    # ---- alias routing --------------------------------------------------------
    def _validate_alias(self, alias: str | None,
                        exclude_id: str | None = None) -> str | None:
        """Normalize + validate a routing alias. Raises ValueError."""
        if alias is None:
            return None
        alias = alias.strip()
        if not alias:
            return None
        if not _ALIAS_RE.match(alias):
            raise ValueError(
                "alias must be letters/digits/._- (max 64 chars)")
        if alias.lower() in _RESERVED_ALIASES:
            raise ValueError(f"alias {alias!r} is reserved")
        for eid, row in self.endpoints.items():
            if eid != exclude_id and (row.get("alias") or "").lower() == alias.lower():
                raise ValueError(
                    f"alias {alias!r} already used by endpoint {row['name']!r}")
        return alias

    def _validate_models(self, models: list[str] | None,
                         exclude_id: str | None = None) -> list[str]:
        """Normalize + validate an ``available_models`` allowlist.

        Entries share the routing namespace with aliases — a client sends one
        as ``"model"`` — so they must not collide with a reserved name, with
        another endpoint's alias, or with another endpoint's allowlist.
        Raises ValueError.
        """
        if not models:
            return []
        seen: dict[str, str] = {}
        for raw in models:
            model = str(raw).strip()
            if not model:
                continue
            if not _MODEL_ID_RE.match(model):
                raise ValueError(
                    f"model id {model!r} has characters that can't appear in a"
                    " routing name (letters/digits and . _ : / @ + -)")
            if model.lower() in _RESERVED_ALIASES:
                raise ValueError(f"model id {model!r} is reserved")
            seen.setdefault(model.lower(), model)  # dedupe, keep first casing

        for eid, row in self.endpoints.items():
            if eid == exclude_id:
                continue
            alias = (row.get("alias") or "").lower()
            if alias and alias in seen:
                raise ValueError(
                    f"model id {seen[alias]!r} is already the alias of"
                    f" endpoint {row['name']!r}")
            for other in parse_models(row.get("available_models")):
                if other.lower() in seen:
                    raise ValueError(
                        f"model id {other!r} is already served by endpoint"
                        f" {row['name']!r}")
        return list(seen.values())

    def available_models(self, eid: str) -> list[str]:
        """The endpoint's declared model allowlist (empty when unset)."""
        row = self.endpoints.get(eid)
        return parse_models(row.get("available_models")) if row else []

    def resolve_alias(self, model: str | None) -> dict | None:
        """Endpoint whose alias matches the requested model name."""
        if not model:
            return None
        wanted = model.strip().lower()
        for row in self.endpoints.values():
            if (row.get("alias") or "").lower() == wanted:
                return row
        return None

    def resolve_request_model(
            self, model: str | None) -> tuple[dict | None, str | None]:
        """Route an inbound ``model`` value to an endpoint.

        Returns ``(endpoint, exact_model)``. Two ways to match, in order:

        1. **Alias** — a routing name for the server itself. The model sent
           upstream is the endpoint's choice (``upstream_model``), so
           ``exact_model`` is None.
        2. **Allowlist entry** — the caller named a real model the endpoint
           declares it serves. That id is forwarded verbatim and outranks the
           endpoint's ``model_override``; picking a specific model is the
           whole point of asking for it by name.
        """
        if not model:
            return None, None
        alias_row = self.resolve_alias(model)
        if alias_row is not None:
            return alias_row, None
        wanted = model.strip().lower()
        for row in self.endpoints.values():
            for candidate in parse_models(row.get("available_models")):
                if candidate.lower() == wanted:
                    return row, candidate
        return None, None

    def upstream_model(self, eid: str) -> str | None:
        """Model to send upstream for alias-routed requests: explicit
        override, else the endpoint's discovered default model."""
        row = self.endpoints.get(eid)
        if row is None:
            return None
        if row.get("model_override"):
            return row["model_override"]
        st = self.state.get(eid)
        return st.model if st else None

    def clear_model_override(self, eid: str) -> str | None:
        """Drop a stale model_override (memory + DB). Returns the removed
        value, or None if there was nothing to clear. Used to self-heal when
        the upstream rejects the pinned model because it was swapped out."""
        row = self.endpoints.get(eid)
        if row is None:
            return None
        prev = row.get("model_override")
        if not prev:
            return None
        row["model_override"] = None
        self.db.execute(
            "UPDATE endpoints SET model_override = NULL WHERE id = ?", (eid,))
        log.warning("cleared stale model_override", extra={"data": {
            "endpoint": row["name"], "was": prev,
            "now_serving": self.state.get(eid).models if self.state.get(eid)
            else []}})
        return prev

    # ---- CRUD --------------------------------------------------------------
    def create(self, spec: EndpointCreate) -> dict:
        eid = str(uuid.uuid4())
        tunnel_command = (spec.tunnel_command or "").strip() or None
        row = {
            "id": eid, "name": spec.name,
            "alias": self._validate_alias(spec.alias),
            "kind": spec.kind or _infer_kind(
                spec.base_url, spec.tunnel_id, tunnel_command),
            "server_type": spec.server_type,
            "protocol": spec.protocol,
            "available_models": _models_json(
                self._validate_models(spec.available_models)),
            "base_url": spec.base_url.rstrip("/"),
            "upstream_key": spec.upstream_key,
            "tunnel_id": spec.tunnel_id,
            "tunnel_command": tunnel_command,
            "tunnel_local_port": spec.tunnel_local_port,
            "priority": spec.priority,
            "weight": spec.weight, "enabled": int(spec.enabled),
            "model_override": spec.model_override,
            "created_ts": time.time(),
        }
        # Positional insert against an explicit column list: the order here
        # must stay in lockstep with the key order of `row` above.
        self.db.execute(
            "INSERT INTO endpoints (id, name, alias, kind, server_type,"
            " protocol, available_models,"
            " base_url, upstream_key, tunnel_id, tunnel_command,"
            " tunnel_local_port, priority, weight, enabled,"
            " model_override, created_ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(row.values()))
        self.endpoints[eid] = row
        self.state[eid] = LiveState()
        # Only an OpenAI-protocol endpoint can become the default pin; an
        # alias-only protocol is never the fallback for `model: "auto"`.
        if self.pinned_id is None and row["protocol"] == "openai":
            self.pinned_id = eid
            self._persist_router()
        log.info("endpoint created", extra={"data": {
            "id": eid, "name": row["name"], "url": row["base_url"],
            "kind": row["kind"]}})
        return row

    def patch(self, eid: str, patch: EndpointPatch) -> dict | None:
        row = self.endpoints.get(eid)
        if row is None:
            return None
        changes = patch.model_dump(exclude_none=True)
        if "alias" in changes:
            changes["alias"] = self._validate_alias(
                changes["alias"], exclude_id=eid)
        if "base_url" in changes:
            changes["base_url"] = changes["base_url"].rstrip("/")
        if "enabled" in changes:
            changes["enabled"] = int(changes["enabled"])
        if "tunnel_command" in changes:
            changes["tunnel_command"] = (
                (changes["tunnel_command"] or "").strip() or None)
        if "available_models" in changes:
            # Row values go straight into the UPDATE below, so the column's
            # storage form (JSON text) is what lands in the in-memory row.
            changes["available_models"] = _models_json(
                self._validate_models(changes["available_models"],
                                      exclude_id=eid))
        row.update(changes)
        row["kind"] = patch.kind or _infer_kind(
            row["base_url"], row.get("tunnel_id"), row.get("tunnel_command"))
        sets = ", ".join(f"{k} = ?" for k in row if k != "id")
        self.db.execute(
            f"UPDATE endpoints SET {sets} WHERE id = ?",
            [v for k, v in row.items() if k != "id"] + [eid])
        if "base_url" in changes:
            self.state[eid] = LiveState()  # URL changed: health unknown again
        elif "available_models" in changes:
            self._apply_allowlist(eid)  # take effect now, not next probe
        log.info("endpoint updated", extra={"data": {"id": eid, **changes}})
        return row

    # ---- registrations -----------------------------------------------------
    def find_by_key(self, key: str) -> dict | None:
        return next((r for r in self.endpoints.values()
                     if r.get("external_key") == key), None)

    def find_unowned_by_url(self, base_url: str) -> dict | None:
        url = base_url.rstrip("/")
        return next((r for r in self.endpoints.values()
                     if not r.get("external_key") and not r.get("owner")
                     and r["base_url"].rstrip("/") == url), None)

    def conflicting_models(self, models: list[str],
                           exclude_id: str | None = None) -> set[str]:
        """Lower-cased ids from ``models`` that another endpoint already
        routes, as an alias or in its allowlist."""
        taken: set[str] = set()
        for eid, row in self.endpoints.items():
            if eid == exclude_id:
                continue
            if row.get("alias"):
                taken.add(row["alias"].lower())
            taken.update(m.lower() for m in parse_models(row.get("available_models")))
        return {m.lower() for m in models} & taken

    def set_ownership(self, eid: str, owner: str | None, key: str | None,
                      meta: dict | None) -> None:
        row = self.endpoints[eid]
        row["owner"], row["external_key"] = owner, key
        row["meta"] = json.dumps(meta) if meta else None
        self.db.execute(
            "UPDATE endpoints SET owner = ?, external_key = ?, meta = ? WHERE id = ?",
            (row["owner"], row["external_key"], row["meta"], eid))

    def delete(self, eid: str) -> bool:
        if eid not in self.endpoints:
            return False
        self.db.execute("DELETE FROM endpoints WHERE id = ?", (eid,))
        self.endpoints.pop(eid)
        self.state.pop(eid, None)
        if self.pinned_id == eid:
            self.pinned_id = next(iter(self.endpoints), None)
            self._persist_router()
        log.info("endpoint deleted", extra={"data": {"id": eid}})
        return True

    # ---- tunnel routes (multiple ssh commands per endpoint) --------------
    # An endpoint keeps its single alias/base_url; each saved route is just a
    # candidate ssh command. Activating one copies it into the endpoint's
    # tunnel_command/tunnel_local_port, which the existing interactive
    # connect flow already reads — so hot-swapping the ssh path needs no
    # changes to tunnel_sessions.py.
    def _route_out(self, row: dict, eid: str) -> dict:
        active_id = self.endpoints.get(eid, {}).get("active_tunnel_route_id")
        return {**row, "active": row["id"] == active_id}

    def list_routes(self, eid: str) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM tunnel_routes WHERE endpoint_id = ?"
            " ORDER BY created_ts", (eid,))
        return [self._route_out(r, eid) for r in rows]

    def get_route(self, eid: str, rid: str) -> dict | None:
        row = self.db.query_one(
            "SELECT * FROM tunnel_routes WHERE id = ? AND endpoint_id = ?",
            (rid, eid))
        return self._route_out(row, eid) if row else None

    def create_route(self, eid: str, label: str, command: str) -> dict:
        if eid not in self.endpoints:
            raise ValueError("endpoint not found")
        validate_command(command)
        rid = str(uuid.uuid4())
        row = {
            "id": rid, "endpoint_id": eid, "label": label.strip(),
            "command": command.strip(),
            "local_port": parse_local_port(command),
            "created_ts": time.time(),
        }
        self.db.execute(
            "INSERT INTO tunnel_routes (id, endpoint_id, label, command,"
            " local_port, created_ts) VALUES (?,?,?,?,?,?)",
            tuple(row.values()))
        log.info("tunnel route created", extra={"data": {
            "endpoint": eid, "route": rid, "label": row["label"]}})
        return self._route_out(row, eid)

    def patch_route(self, eid: str, rid: str, label: str | None,
                    command: str | None) -> dict | None:
        row = self.db.query_one(
            "SELECT * FROM tunnel_routes WHERE id = ? AND endpoint_id = ?",
            (rid, eid))
        if row is None:
            return None
        if command is not None:
            validate_command(command)
            row["command"] = command.strip()
            row["local_port"] = parse_local_port(command)
        if label is not None:
            row["label"] = label.strip()
        self.db.execute(
            "UPDATE tunnel_routes SET label = ?, command = ?, local_port = ?"
            " WHERE id = ?",
            (row["label"], row["command"], row["local_port"], rid))
        if self.endpoints.get(eid, {}).get("active_tunnel_route_id") == rid:
            self._apply_route_to_endpoint(eid, row)
        log.info("tunnel route updated", extra={"data": {
            "endpoint": eid, "route": rid}})
        return self._route_out(row, eid)

    def delete_route(self, eid: str, rid: str) -> bool:
        n = self.db.execute(
            "DELETE FROM tunnel_routes WHERE id = ? AND endpoint_id = ?",
            (rid, eid))
        if n and self.endpoints.get(eid, {}).get("active_tunnel_route_id") == rid:
            row = self.endpoints[eid]
            row["active_tunnel_route_id"] = None
            self.db.execute(
                "UPDATE endpoints SET active_tunnel_route_id = NULL"
                " WHERE id = ?", (eid,))
        log.info("tunnel route deleted", extra={"data": {
            "endpoint": eid, "route": rid}})
        return bool(n)

    def _apply_route_to_endpoint(self, eid: str, route: dict) -> None:
        row = self.endpoints[eid]
        row["tunnel_command"] = route["command"]
        row["tunnel_local_port"] = route["local_port"]
        row["active_tunnel_route_id"] = route["id"]
        row["kind"] = _infer_kind(
            row["base_url"], row.get("tunnel_id"), row["tunnel_command"])
        self.db.execute(
            "UPDATE endpoints SET tunnel_command = ?, tunnel_local_port = ?,"
            " active_tunnel_route_id = ?, kind = ? WHERE id = ?",
            (row["tunnel_command"], row["tunnel_local_port"],
             row["active_tunnel_route_id"], row["kind"], eid))

    def activate_route(self, eid: str, rid: str) -> dict | None:
        if eid not in self.endpoints:
            return None
        row = self.db.query_one(
            "SELECT * FROM tunnel_routes WHERE id = ? AND endpoint_id = ?",
            (rid, eid))
        if row is None:
            return None
        self._apply_route_to_endpoint(eid, row)
        log.info("tunnel route activated", extra={"data": {
            "endpoint": eid, "route": rid, "label": row["label"]}})
        return self.endpoints[eid]

    # ---- policy / hot-swap ---------------------------------------------------
    def activate(self, eid: str) -> bool:
        """Hot-swap: pin as the active endpoint. Next request routes here."""
        if eid not in self.endpoints:
            return False
        prev = self.pinned_id
        self.pinned_id = eid
        self._persist_router()
        log.info("hot-swap", extra={"data": {
            "from": self.endpoints.get(prev, {}).get("name") if prev else None,
            "to": self.endpoints[eid]["name"]}})
        return True

    def set_policy(self, policy: str | None, pinned_id: str | None) -> None:
        if policy:
            self.policy = policy
        if pinned_id is not None:
            if pinned_id and pinned_id not in self.endpoints:
                raise KeyError(pinned_id)
            self.pinned_id = pinned_id or None
        self._persist_router()
        log.info("router policy updated", extra={"data": {
            "policy": self.policy, "pinned": self.pinned_id}})

    # ---- resolution -----------------------------------------------------------
    def _eligible(self, exclude: set[str]) -> list[dict]:
        # Non-OpenAI protocols are alias-only: an agent server is not a
        # drop-in substitute for a raw llama.cpp box, so it must never be
        # picked for `model: "auto"` or as a silent failover target. Reaching
        # one is always something the caller asked for by name.
        rows = [
            r for r in self.endpoints.values()
            if r["enabled"] and r["id"] not in exclude
            and (r.get("protocol") or "openai") == "openai"
            and self.state[r["id"]].health != "failed"
        ]
        rows.sort(key=lambda r: (-r["priority"], r["created_ts"] or 0))
        return rows

    def resolve(self, exclude: set[str] | None = None,
                auto_failover: bool = True) -> dict | None:
        """Pick the upstream for a request, honoring pin + failover."""
        exclude = exclude or set()
        if self.policy == "manual" and self.pinned_id:
            pinned = self.endpoints.get(self.pinned_id)
            # Same alias-only rule as _eligible: pinning an agent server must
            # not silently redirect every `model: "auto"` request into it.
            if pinned and (pinned.get("protocol") or "openai") != "openai":
                pinned = None
            if pinned and pinned["enabled"] and self.pinned_id not in exclude:
                if self.state[self.pinned_id].health != "failed":
                    return pinned
                if not auto_failover:
                    return pinned  # pinned hard: let the request surface the error
        candidates = self._eligible(exclude)
        if self.policy == "manual" and not auto_failover:
            return None
        return candidates[0] if candidates else None

    # ---- health signals ---------------------------------------------------------
    def report_success(self, eid: str, latency_ms: float | None = None,
                       source: str = "request") -> None:
        st = self.state.get(eid)
        if st is None:
            return
        prev = st.health
        st.last_ok_ts = time.time()
        if source == "probe":
            st.last_probe_ok = True
        if latency_ms is not None:
            st.ewma_latency_ms = (
                latency_ms if st.ewma_latency_ms is None
                else 0.3 * latency_ms + 0.7 * st.ewma_latency_ms)
        if st.health == "failed":
            if source == "probe":
                st.consecutive_probe_ok += 1
                if st.consecutive_probe_ok >= self.recover_after:
                    st.health = "healthy"
                    st.consecutive_fails = 0
            # request successes alone don't recover a FAILED endpoint
        else:
            st.health = "healthy"
            st.consecutive_fails = 0
            st.consecutive_probe_ok += 1
        if prev != st.health:
            log.info("health transition", extra={"data": {
                "endpoint": self.endpoints[eid]["name"], "from": prev,
                "to": st.health, "source": source}})

    def report_failure(self, eid: str, error: str, source: str = "request") -> None:
        """Feed a failure into the state machine.

        Only the *prober* can take an endpoint all the way to FAILED. Request
        failures degrade it and no more, for as long as probes keep passing.

        That asymmetry is deliberate, and it mirrors the one on the recovery
        side (a successful request never clears FAILED — only probes do).
        Under congestion an overloaded but perfectly alive model server
        returns a run of timeouts; three of those used to drop the only
        endpoint out of the pool, at which point every subsequent request got
        "no healthy upstream" and the relay was down while the server behind
        it was merely busy. A server that is genuinely gone fails its probes
        too, and goes FAILED on the next sweep — a few seconds later, with
        evidence.
        """
        st = self.state.get(eid)
        if st is None:
            return
        prev = st.health
        st.consecutive_fails += 1
        st.consecutive_probe_ok = 0
        if source == "probe":
            st.last_probe_ok = False
        enough = st.consecutive_fails >= self.unhealthy_after
        if enough and st.last_probe_ok is not True:
            st.health = "failed"
        else:
            st.health = "degraded"
        level = log.warning if st.health != prev else log.debug
        level("endpoint failure", extra={"data": {
            "endpoint": self.endpoints[eid]["name"], "error": error[:300],
            "fails": st.consecutive_fails, "from": prev, "to": st.health,
            "source": source, "last_probe_ok": st.last_probe_ok}})

    def set_models(self, eid: str, models: list[str]) -> None:
        """Record what a probe discovered, then narrow it to the allowlist.

        The raw list is kept so the allowlist can be re-applied when it is
        edited, without waiting for the next probe.
        """
        st = self.state.get(eid)
        if st is None:
            return
        st.discovered = models
        self._apply_allowlist(eid)

    def sync_allowlist(self, eid: str, models: list[str]) -> bool:
        """Publish a discovered model list as the endpoint's allowlist.

        For an alias-only protocol like OpenCode, ``available_models`` is not
        a restriction so much as the *addressable* list: a model that isn't in
        it cannot be named in a request at all (see ``resolve_request_model``)
        and never appears in ``/v1/models``. Discovery and addressability have
        to be the same set, or the models the dashboard shows as available are
        ones nobody can actually call.

        Called from the boot reconcile only, never from the probe loop — an
        allowlist narrowed by hand in the dashboard should survive until the
        next restart rather than be overwritten fifteen seconds later.
        """
        row = self.endpoints.get(eid)
        if row is None:
            return False
        current = self.available_models(eid)
        if [m.lower() for m in current] == [m.lower() for m in models]:
            return False
        try:
            validated = self._validate_models(models, exclude_id=eid)
        except ValueError as e:
            log.warning("could not publish discovered models", extra={"data": {
                "endpoint": row["name"], "error": str(e)}})
            return False
        row["available_models"] = _models_json(validated)
        self.db.execute(
            "UPDATE endpoints SET available_models = ? WHERE id = ?",
            (row["available_models"], eid))
        self._apply_allowlist(eid)
        log.info("published discovered models", extra={"data": {
            "endpoint": row["name"], "models": validated}})
        return True

    def _apply_allowlist(self, eid: str) -> None:
        st = self.state[eid]
        allowed = self.available_models(eid)
        if not allowed:
            st.models = list(st.discovered)
        else:
            # Intersection in allowlist order — the operator's ordering is the
            # one that decides the default model. An allowlisted id the probe
            # didn't report is still honored: OpenCode's provider catalog can
            # legitimately lag what a provider will actually serve.
            discovered = {m.lower() for m in st.discovered}
            matched = [m for m in allowed if m.lower() in discovered]
            st.models = matched or allowed
        st.model = st.models[0] if st.models else None

    # ---- views -----------------------------------------------------------------
    def out(self, eid: str, share: float = 0.0) -> EndpointOut:
        row = self.endpoints[eid]
        st = self.state[eid]
        return EndpointOut(
            id=row["id"], name=row["name"], alias=row.get("alias"),
            kind=row["kind"],
            server_type=row["server_type"],
            protocol=row.get("protocol") or "openai",
            available_models=self.available_models(eid),
            base_url=row["base_url"],
            has_key=bool(row["upstream_key"]), tunnel_id=row["tunnel_id"],
            tunnel_command=row.get("tunnel_command"),
            tunnel_local_port=row.get("tunnel_local_port"),
            priority=row["priority"], weight=row["weight"],
            enabled=bool(row["enabled"]), model=st.model,
            model_override=row.get("model_override"), health=st.health,
            ewma_latency_ms=(
                round(st.ewma_latency_ms, 1) if st.ewma_latency_ms else None),
            last_ok_ts=st.last_ok_ts, consecutive_fails=st.consecutive_fails,
            active=(row["id"] == self.pinned_id), share=round(share, 4),
            owner=row.get("owner"), external_key=row.get("external_key"),
            meta=_meta(row.get("meta")))

    def list_out(self, shares: dict[str, float] | None = None) -> list[EndpointOut]:
        shares = shares or {}
        return [self.out(eid, shares.get(eid, 0.0)) for eid in self.endpoints]

    def router_state(self, auto_failover: bool = True) -> RouterState:
        resolved = self.resolve(auto_failover=auto_failover)
        return RouterState(
            policy=self.policy, pinned_id=self.pinned_id,
            resolved_id=resolved["id"] if resolved else None,
            resolved_name=resolved["name"] if resolved else None)
