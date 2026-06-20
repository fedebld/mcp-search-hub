#!/usr/bin/env bash
# Wrapper: lancia how_to_use.py col venv che contiene il pacchetto 'mcp'.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="${HUB_VENV_PY:-/home/llmadmin/venv/bin/python3}"
exec "$PY" "$DIR/how_to_use.py" "$@"
