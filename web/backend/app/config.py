"""Static (boot-time) configuration via environment variables.

Runtime-mutable settings (toggles, retention, proxy port persisted from the
dashboard) live in the DB behind ``app.services.settings_store``; this module
is only for values that must exist before the DB is open.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent  # web/backend
REPO_ROOT = BACKEND_DIR.parent.parent
# The repo-root .env, not web/backend/.env: everything else (launch.sh, the
# systemd wrappers) reads that one, and a plain `uvicorn app.main:app` run from
# web/backend should pick up the same file rather than silently ignore it.
ENV_FILE = REPO_ROOT / ".env"


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RELAY_", env_file=ENV_FILE, extra="ignore"
    )

    # This machine only by default: there is no auth layer, and an OpenCode
    # endpoint behind relay runs shell commands for whoever calls it. Set
    # RELAY_HOST=0.0.0.0 to serve other machines — see docs/deployment.md.
    host: str = "127.0.0.1"
    port: int = 4000

    db_path: Path = BACKEND_DIR / "data" / "relay.db"
    log_dir: Path = BACKEND_DIR / "logs"
    log_level: str = "INFO"

    # Health prober
    probe_interval: float = 15.0
    probe_timeout: float = 5.0
    unhealthy_after: int = 3  # consecutive fails -> FAILED (out of rotation)
    recover_after: int = 2  # consecutive probe successes -> back to HEALTHY

    # Proxy timeouts (separate connect/read so slow streams != dead connects)
    connect_timeout: float = 10.0
    read_timeout: float = 600.0
    write_timeout: float = 60.0

    # How many attempts relay has at an upstream simultaneously. This bounds
    # the *model server*, not the number of clients relay can hold: arrivals
    # past this wait in a bounded queue (below) or are shed with 429. See
    # docs/deployment.md for sizing.
    max_concurrency: int = 256
    # Requests allowed to wait for a slot. max_concurrency + queue_limit is
    # the number of live requests relay carries before it starts refusing;
    # the defaults hold ~2300, comfortably past the 1024-connection target.
    queue_limit: int = 2048
    # Seconds a queued request may wait before relay answers 429 itself. A
    # caller that will not be served for a minute is better told now.
    queue_timeout: float = 30.0
    # Hard cap on an inbound request body. `Request.body()` is unbounded, so
    # without this one large POST is a memory incident.
    max_body_bytes: int = 8 * 1024 * 1024
    # GAIFA sets it (plan llm-servers 1.7): a request to a cloud endpoint must
    # say `X-GAIFA-Purpose: chat`, so only chats reach paid providers.
    cloud_requires_purpose: bool = False

    # Upstream HTTP connection pool. Sized from max_concurrency at boot (see
    # main.lifespan) unless set explicitly: a pool smaller than the gate lets
    # PoolTimeout — a *local* failure — masquerade as an unhealthy endpoint.
    pool_connections: int = 0  # 0 = derive from max_concurrency
    pool_keepalive: int = 0    # 0 = derive from max_concurrency
    pool_timeout: float = 10.0

    # Threads serving SQLite. Its own pool, not asyncio's shared default
    # executor: dashboard queries and telemetry writes must not be able to
    # starve each other or anything else that offloads to a thread.
    db_threads: int = 8

    frontend_dist: Path = BACKEND_DIR.parent / "frontend" / "dist"


class OpenCodeConfig(BaseSettings):
    """The OpenCode agent server relay registers for itself at boot.

    Nothing here has to be set: the defaults describe an ``opencode serve`` on
    this machine at :4096, and the Basic password is read from the credentials
    file ``scripts/opencode-auth.sh`` mints on first launch. That file is the
    only reason a clone-and-run works without touching a config.
    """

    model_config = SettingsConfigDict(
        env_prefix="OPENCODE_", env_file=ENV_FILE, extra="ignore"
    )

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 4096

    endpoint_name: str = "opencode"
    alias: str = "agent"  # what clients send as "model" to reach the agent

    server_username: str = "opencode"
    server_password: str = ""
    auth_file: Path = BACKEND_DIR / "data" / "opencode-auth.env"

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def credential(self) -> str:
        """``user:password`` for the adapter's HTTP Basic header, or "" when
        no password is known. Environment wins over the generated file so a
        hand-set OPENCODE_SERVER_PASSWORD is always what is presented."""
        stored = _read_env_file(self.auth_file)
        user = self.server_username or stored.get(
            "OPENCODE_SERVER_USERNAME", "opencode")
        password = self.server_password or stored.get(
            "OPENCODE_SERVER_PASSWORD", "")
        return f"{user}:{password}" if password else ""


def _read_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=value reader for the generated credentials file. Not a
    dotenv parser — it only ever reads what opencode-auth.sh writes."""
    out: dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip("'\"")
    return out


config = Config()
opencode_config = OpenCodeConfig()
