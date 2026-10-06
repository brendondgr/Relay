#!/usr/bin/env bash
# Builds the relay's venv (web/backend/.venv) for AIHub (run by `aihub install relay`). The
# dashboard (web/frontend/dist) is built with npm; run `npm ci && npm run build` in web/frontend
# after changing it.
set -euo pipefail
cd "$(dirname "$0")/web/backend"
UV_PYTHON_PREFERENCE=only-managed uv sync --quiet --python 3.12
if [[ ! -d ../frontend/dist ]] && command -v npm >/dev/null; then
  (cd ../frontend && npm ci --silent && npm run build --silent)
fi
echo "installed relay into web/backend/.venv"
