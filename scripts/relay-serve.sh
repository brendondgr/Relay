#!/usr/bin/env bash
# ExecStart target for relay.service.
#
# A wrapper rather than a hardcoded `uvicorn --host X --port Y` so that the
# bind address and port come from .env like everything else. The unit file used
# to spell them out, which meant any local change to the host had to be made by
# editing the installed unit — and was then silently reverted the next time
# install-systemd.sh ran.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$ROOT/.env"
  set +a
fi

cd "$ROOT/web/backend"
exec /usr/bin/uv run uvicorn app.main:app \
  --host "${RELAY_HOST:-127.0.0.1}" \
  --port "${RELAY_PORT:-4000}"
