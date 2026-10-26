"""Pydantic models shared by routes and services (the API contract)."""

from typing import Literal

from pydantic import BaseModel, Field

# "cloud": a paid provider's API (Anthropic, OpenAI, Gemini). Alias-only like the
# non-OpenAI protocols: never the `auto` route or a failover target, never GAIFA's GPU slot.
EndpointKind = Literal["local", "remote_direct", "remote_tunnel", "cloud"]
ServerType = Literal["llama.cpp", "vLLM", "ollama", "openai", "opencode", "anthropic", "gemini"]
# Wire protocol the upstream speaks. Dispatches services/adapters/; the only
# field any backend code branches on to decide how to talk to a server.
Protocol = Literal["openai", "opencode", "anthropic", "gemini"]
# A provider's API, filled in by `preset` on create: protocol, URL, badge, alias.
Preset = Literal["anthropic", "openai", "gemini"]
PRESETS: dict[str, dict] = {
    "anthropic": {"protocol": "anthropic", "base_url": "https://api.anthropic.com", "server_type": "anthropic", "alias": "claude"},
    "openai": {"protocol": "openai", "base_url": "https://api.openai.com/v1", "server_type": "openai", "alias": "openai"},
    "gemini": {"protocol": "gemini", "base_url": "https://generativelanguage.googleapis.com", "server_type": "gemini", "alias": "gemini"},
}
CLOUD_HOSTS = ("api.anthropic.com", "api.openai.com", "generativelanguage.googleapis.com")
Health = Literal["healthy", "degraded", "failed", "unknown"]
RouterPolicy = Literal["manual", "priority"]


# ---- model facts (services/model_facts.py) ---------------------------
class ModelFactsOverride(BaseModel):
    """What the operator knows better than the upstream says. Each field set
    here wins over the detected value; null fields fall through."""
    context_length: int | None = Field(default=None, ge=1, le=100_000_000)
    tools: bool | None = None
    vision: bool | None = None
    reasoning: bool | None = None


class ModelMeta(BaseModel):
    """``meta`` on each ``GET /v1/models`` entry. null = unknown."""
    context_length: int | None = None
    tools: bool | None = None
    vision: bool | None = None
    reasoning: bool | None = None
    # Where context_length came from (else where the known facts came from).
    source: Literal["override", "props", "vllm", "probe"] | None = None


class ModelFactsOut(BaseModel):
    """GET /admin/endpoints/{id}/facts."""
    endpoint_id: str
    model: str | None = None
    meta: ModelMeta
    detected: ModelMeta  # before the override
    checked_ts: float | None = None


# ---- endpoints -------------------------------------------------------
class EndpointCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    # A provider's API in one field: fills protocol, base_url, server_type and
    # the alias (each still overridable); its kind is "cloud".
    preset: Preset | None = None
    base_url: str = Field(default="", max_length=2000)
    alias: str | None = Field(default=None, max_length=64)
    kind: EndpointKind | None = None  # inferred from URL when omitted
    server_type: ServerType = "openai"
    protocol: Protocol = "openai"
    # Explicit allowlist of model ids this endpoint serves. Clients may send
    # any of them as "model" to reach this endpoint with that exact model, and
    # they are advertised through GET /v1/models. Empty = advertise nothing
    # extra and use whatever the endpoint reports.
    available_models: list[str] = Field(default_factory=list, max_length=200)
    upstream_key: str | None = None
    tunnel_id: str | None = None
    # Interactive SSH tunnel: a raw `ssh -N -L ...` command run on demand.
    tunnel_command: str | None = None
    tunnel_local_port: int | None = None
    priority: int = 100
    weight: int = 1
    enabled: bool = True
    model_override: str | None = None  # model sent upstream for alias routes
    facts_override: ModelFactsOverride | None = None
    # The catalog lists this endpoint's own models as `<alias>/<model>`
    # (null: on for cloud endpoints and the anthropic/gemini protocols).
    list_models: bool | None = None


class EndpointPatch(BaseModel):
    name: str | None = None
    base_url: str | None = None
    alias: str | None = None
    kind: EndpointKind | None = None
    server_type: ServerType | None = None
    protocol: Protocol | None = None
    available_models: list[str] | None = Field(default=None, max_length=200)
    upstream_key: str | None = None
    tunnel_id: str | None = None
    tunnel_command: str | None = None
    tunnel_local_port: int | None = None
    priority: int | None = None
    weight: int | None = None
    enabled: bool | None = None
    model_override: str | None = None
    # Replaces the whole override; send {} to clear it.
    facts_override: ModelFactsOverride | None = None
    list_models: bool | None = None


class EndpointOut(BaseModel):
    id: str
    name: str
    alias: str | None = None
    kind: EndpointKind
    server_type: ServerType
    protocol: Protocol = "openai"
    available_models: list[str] = []
    base_url: str
    has_key: bool
    key_hint: str | None = None      # the key's last four characters; the key itself is never returned
    tunnel_id: str | None
    tunnel_command: str | None = None
    tunnel_local_port: int | None = None
    priority: int
    weight: int
    enabled: bool
    model: str | None = None
    model_override: str | None = None
    facts_override: ModelFactsOverride | None = None
    health: Health = "unknown"
    ewma_latency_ms: float | None = None
    last_ok_ts: float | None = None
    consecutive_fails: int = 0
    active: bool = False
    share: float = 0.0
    # Set when another program registered this endpoint (see Registration*).
    owner: str | None = None
    external_key: str | None = None
    meta: dict | None = None
    list_models: bool = False
    hidden_models: list[str] = []
    hide_filter: bool = True


class ModelsPatch(BaseModel):
    """PATCH /admin/endpoints/{id}/models: which of its listed models the catalog leaves out."""
    hidden: list[str] | None = Field(default=None, max_length=2000)
    hide_filter: bool | None = None


# ---- registrations ---------------------------------------------------
class RegistrationUpsert(BaseModel):
    """PUT /admin/registrations/{key}: make an endpoint exist, idempotently.

    Give ``port`` for a server on this machine (``host`` defaults to
    127.0.0.1 and the URL becomes ``http://host:port/v1``), or a full
    ``base_url``.
    """
    name: str = Field(min_length=1, max_length=120)
    port: int | None = Field(default=None, ge=1, le=65535)
    host: str = Field(default="127.0.0.1", max_length=253)
    base_url: str | None = None
    alias: str | None = Field(default=None, max_length=64)
    available_models: list[str] = Field(default_factory=list, max_length=200)
    server_type: ServerType = "llama.cpp"
    owner: str = Field(default="external", min_length=1, max_length=64)
    meta: dict = Field(default_factory=dict)


class RegistrationOut(BaseModel):
    key: str
    owner: str | None
    # True when the key matched no row and an unowned endpoint with the same
    # base_url was taken over (its id, name, alias and history are kept).
    adopted: bool = False
    # Requested model ids another endpoint already serves; not advertised.
    skipped_models: list[str] = []
    endpoint: EndpointOut


class TunnelRouteCreate(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    command: str = Field(min_length=1)


class TunnelRoutePatch(BaseModel):
    label: str | None = None
    command: str | None = None


class TunnelRouteOut(BaseModel):
    id: str
    endpoint_id: str
    label: str
    command: str
    local_port: int | None = None
    active: bool = False
    created_ts: float | None = None


class TunnelRouteTestResult(BaseModel):
    ok: bool
    latency_ms: float | None = None
    error: str | None = None


# ---- ssh config hosts ------------------------------------------------
class SshHostOut(BaseModel):
    """A ``~/.ssh/config`` alias resolved via ``ssh -G`` to its real settings."""
    alias: str
    hostname: str | None = None
    user: str | None = None
    port: int = 22
    identity_files: list[str] = []
    identity_file: str | None = None   # the effective one (explicit if any)
    identity_explicit: bool = False    # a specific key vs. default fan-out
    proxyjump: str | None = None


class EndpointTestResult(BaseModel):
    ok: bool
    latency_ms: float | None = None
    models: list[str] = []
    error: str | None = None


class RouterState(BaseModel):
    policy: RouterPolicy
    pinned_id: str | None
    resolved_id: str | None
    resolved_name: str | None


class RouterUpdate(BaseModel):
    policy: RouterPolicy | None = None
    pinned_id: str | None = None


# ---- tunnels ---------------------------------------------------------
class TunnelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    ssh_host: str
    ssh_port: int = 22
    ssh_user: str
    key_path: str | None = None
    remote_host: str = "127.0.0.1"
    remote_port: int
    local_port: int
    compress: bool = True
    keepalive: bool = True
    extra_opts: str | None = None
    enabled: bool = False


class TunnelPatch(BaseModel):
    name: str | None = None
    ssh_host: str | None = None
    ssh_port: int | None = None
    ssh_user: str | None = None
    key_path: str | None = None
    remote_host: str | None = None
    remote_port: int | None = None
    local_port: int | None = None
    compress: bool | None = None
    keepalive: bool | None = None
    extra_opts: str | None = None
    enabled: bool | None = None


class TunnelOut(BaseModel):
    id: str
    name: str
    ssh_host: str
    ssh_port: int
    ssh_user: str
    key_path: str | None
    remote_host: str
    remote_port: int
    local_port: int
    compress: bool
    keepalive: bool
    extra_opts: str | None
    enabled: bool
    status: Literal["stopped", "starting", "up", "error"] = "stopped"
    pid: int | None = None
    last_error: str | None = None
    started_at: float | None = None
    uptime_s: float = 0.0


class TunnelTestResult(BaseModel):
    ssh_ok: bool
    ssh_latency_ms: float | None = None
    endpoint_ok: bool = False
    endpoint_latency_ms: float | None = None
    models: list[str] = []
    error: str | None = None


TunnelSessionState = Literal[
    "idle", "connecting", "awaiting_input", "up", "error", "stopped"]


class TunnelSessionStatus(BaseModel):
    """Live state of an endpoint's interactive SSH tunnel session."""
    endpoint_id: str
    status: TunnelSessionState = "idle"
    prompt: str | None = None        # text ssh is waiting on, when awaiting_input
    prompt_secret: bool = False      # mask the answer (password/passphrase)
    output: list[str] = []           # recent, redacted terminal output
    last_error: str | None = None
    local_port: int | None = None
    pid: int | None = None
    uptime_s: float = 0.0


class TunnelRespond(BaseModel):
    text: str = Field(max_length=4096)


# ---- runtime settings ------------------------------------------------
class RuntimeSettings(BaseModel):
    stream_passthrough: bool = True
    queue_requests: bool = True
    auto_failover: bool = True
    log_bodies: bool = False
    allow_cors: bool = True
    inject_stream_usage: bool = True
    # GAIFA plan 31.4: requests to a local engine wait for the hub's GPU slot (no GAIFA_URL: nothing waits)
    gpu_slot: bool = True
    proxy_port: int = 4000
    retention_days: int = 30
    restart_required: bool = False


class SettingsPatch(BaseModel):
    stream_passthrough: bool | None = None
    queue_requests: bool | None = None
    auto_failover: bool | None = None
    log_bodies: bool | None = None
    allow_cors: bool | None = None
    inject_stream_usage: bool | None = None
    gpu_slot: bool | None = None
    proxy_port: int | None = None
    retention_days: int | None = None


# ---- frontend log ingestion -------------------------------------------
class FrontendLogEvent(BaseModel):
    ts: float | None = None
    level: Literal["debug", "info", "warn", "error"] = "info"
    event: str = Field(min_length=1, max_length=200)
    detail: str | None = Field(default=None, max_length=4000)


class FrontendLogBatch(BaseModel):
    events: list[FrontendLogEvent] = Field(max_length=200)
