#!/usr/bin/env bash
# Container entrypoint: render the OpenCode project config from the
# environment, then hand off to the Memory Builder web app.
set -euo pipefail

cd "$(dirname "$0")/.."

BARYGRAPH_MCP_URL="${BARYGRAPH_MCP_URL:-http://127.0.0.1:8000/mcp}"
export BARYGRAPH_MCP_URL

if [ -z "${NVIDIA_API_KEY:-}" ]; then
  echo "warning: NVIDIA_API_KEY is not set; NVIDIA models will not authenticate" >&2
else
  echo "nvidia: NVIDIA_API_KEY is set" >&2
fi
echo "barygraph MCP: ${BARYGRAPH_MCP_URL}" >&2

# The image ships a project opencode.json for local use; inside the container
# the BaryGraph endpoint is a deployment concern, so render it at startup.
python3 - <<'PY'
import json
import os

config = {
    "$schema": "https://opencode.ai/config.json",
    "mcp": {
        "servers": {
            "barygraph": {
                "type": "remote",
                "url": os.environ["BARYGRAPH_MCP_URL"],
                "oauth": False,
            }
        }
    },
}

with open("opencode.json", "w", encoding="utf-8") as fh:
    json.dump(config, fh, indent=2)
    fh.write("\n")
PY

mkdir -p .state output
exec ./run.sh "$@"
