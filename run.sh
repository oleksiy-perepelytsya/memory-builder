#!/usr/bin/env bash
# Start the Memory Builder web app.
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m uvicorn app.main:app --host "${MEMORY_BUILDER_HOST:-127.0.0.1}" --port "${MEMORY_BUILDER_PORT:-8765}" "$@"
