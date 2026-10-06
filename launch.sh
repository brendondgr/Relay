#!/usr/bin/env bash
# launch.sh — bring up relay and an OpenCode agent server together.
#
#   git clone … && cd DashLLM && ./launch.sh
#
# No configuration required. The Basic credentials `opencode serve` demands are
# generated on first run and kept in web/backend/data/; relay registers the
# agent endpoint itself and discovers the free models the server offers.
#
#   ./launch.sh --takeover      # stop the relay systemd service first
#   ./launch.sh --env-file PATH # load overrides from PATH (default: ./.env)
#
# Everything in .env is optional — see .env.example for the knobs. Both
# processes run in the foreground as children of this script: Ctrl-C stops
# both. For a boot-time setup use the systemd units instead
# (scripts/install-systemd.sh) — they read the same files.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

TAKEOVER=0
ENV_FILE=".env"
while [ $# -gt 0 ]; do
  case "$1" in
    --takeover) TAKEOVER=1 ;;
    --env-file) ENV_FILE="${2:?--env-file needs a path}"; shift ;;
    --env-file=*) ENV_FILE="${1#*=}" ;;
    # the header comment block above is the help text
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

# ---- config -------------------------------------------------------------
# .env is optional. set -a exports what it does define, so the values reach
# both children: relay reads RELAY_*/OPENCODE_*, and `opencode serve` reads
# OPENCODE_SERVER_* plus whatever provider key its providers look for.
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

RELAY_PORT="${RELAY_PORT:-4000}"
RELAY_HOST="${RELAY_HOST:-127.0.0.1}"
OPENCODE_ENABLED="${OPENCODE_ENABLED:-1}"
OPENCODE_PORT="${OPENCODE_PORT:-4096}"
OPENCODE_PROJECT_DIR="${OPENCODE_PROJECT_DIR:-$ROOT}"
export RELAY_PORT RELAY_HOST OPENCODE_PORT

say() { printf '\033[1;33m▸\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m✕\033[0m %s\n' "$*" >&2; exit 1; }

port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

wait_for() { # wait_for <url> <label> [curl args...]
  local url="$1" label="$2"; shift 2
  for _ in $(seq 1 60); do
    curl -fsS --max-time 2 "$@" "$url" >/dev/null 2>&1 && return 0
    sleep 0.5
  done
  die "$label never became reachable at $url"
}

# ---- preflight ----------------------------------------------------------
if [ "$OPENCODE_ENABLED" = "1" ]; then
  command -v opencode >/dev/null || die "opencode is not on PATH (https://opencode.ai)"
  [ -d "$OPENCODE_PROJECT_DIR" ] || die \
    "OPENCODE_PROJECT_DIR does not exist: $OPENCODE_PROJECT_DIR"
  port_busy "$OPENCODE_PORT" && die \
    "port $OPENCODE_PORT is already in use (OPENCODE_PORT)"
  # shellcheck disable=SC1091
  . "$ROOT/scripts/opencode-auth.sh"
fi

if port_busy "$RELAY_PORT"; then
  if [ "$TAKEOVER" = "1" ]; then
    say "stopping the relay service to free :$RELAY_PORT"
    systemctl --user stop relay.service 2>/dev/null || true
    sleep 1
  else
    die "port $RELAY_PORT is already in use.
   If that is the relay systemd service, either use it as-is, or run:
     ./launch.sh --takeover"
  fi
fi

# ---- children -----------------------------------------------------------
pids=()
cleanup() {
  local status=$?
  for pid in "${pids[@]:-}"; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
  exit "$status"
}
trap cleanup EXIT INT TERM

if [ "$OPENCODE_ENABLED" = "1" ]; then
  say "starting opencode serve on :$OPENCODE_PORT (project: $OPENCODE_PROJECT_DIR)"
  # `cd` in a subshell: `opencode serve` binds to its launch directory, which
  # is the project its bash/edit/write tools act on.
  (cd "$OPENCODE_PROJECT_DIR" && exec opencode serve --port "$OPENCODE_PORT") &
  pids+=("$!")
  wait_for "http://127.0.0.1:$OPENCODE_PORT/global/health" "opencode" \
    -u "$OPENCODE_SERVER_USERNAME:$OPENCODE_SERVER_PASSWORD"
  say "opencode healthy"
fi

say "starting relay on $RELAY_HOST:$RELAY_PORT"
(cd "$ROOT/web/backend" && exec uv run uvicorn app.main:app \
  --host "$RELAY_HOST" --port "$RELAY_PORT") &
pids+=("$!")
wait_for "http://127.0.0.1:$RELAY_PORT/health" "relay"
say "relay healthy"

# ---- summary ------------------------------------------------------------
# relay registers the agent endpoint at boot and then discovers its models in
# the background, so give that a moment before printing what is routable.
models=""
for _ in $(seq 1 40); do
  # No f-string here on purpose: escaping a quote inside one is a syntax error
  # before Python 3.12, and this line already lives inside single quotes.
  models=$(curl -fsS --max-time 3 "http://127.0.0.1:$RELAY_PORT/v1/models" \
    2>/dev/null | python3 -c '
import json, sys
data = json.load(sys.stdin)["data"]
print("\n".join("    " + m["id"] for m in data))' 2>/dev/null) || models=""
  case "$models" in *"/"*) break ;; esac
  sleep 0.5
done
[ -n "$models" ] || models="    (none yet — is opencode serve reachable?)"

lan_ip=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')
cat <<EOF

  dashboard   http://127.0.0.1:$RELAY_PORT
  base_url    http://127.0.0.1:$RELAY_PORT/v1${lan_ip:+
  from LAN    http://$lan_ip:$RELAY_PORT/v1}
  auth        none — send a request, no key

  routable models:
$models

  try it:
    curl http://127.0.0.1:$RELAY_PORT/v1/chat/completions \\
      -H 'Content-Type: application/json' \\
      -d '{"model": "${OPENCODE_ALIAS:-agent}", "messages": [{"role": "user", "content": "hi"}]}'

  Ctrl-C stops both servers.

EOF

wait
