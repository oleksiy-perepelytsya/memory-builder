"""Application configuration and filesystem paths.

All paths are resolved relative to the project root so the app can be moved
around without editing code. Environment variables override the defaults.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"
OUTPUT_DIR = BASE_DIR / "output"
# Generated `.jsonl` runs live here so they can be sorted/deleted separately.
RUNS_DIR = OUTPUT_DIR / "runs"
# Curated records kept aside from generated runs.
EXAMPLES_FILE = OUTPUT_DIR / "examples.jsonl"
STATE_DIR = BASE_DIR / ".state"
JOBS_DIR = STATE_DIR / "jobs"

# --- Identity pilot ---------------------------------------------------------
# The pilot has its own output tree and construct memory so it cannot disturb
# the normal Memory Builder run files.
PILOT_DIR = OUTPUT_DIR / "pilot"
PILOT_RUNS_DIR = PILOT_DIR / "runs"
PILOT_MEMORY_FILE = PILOT_DIR / "constructs.jsonl"
PILOT_PROMPTS_FILE = STATE_DIR / "pilot_prompts.json"
PILOT_JOBS_DIR = STATE_DIR / "pilot_jobs"

# The custom OpenCode agent whose system prompt is the "overridden default prompt".
AGENT_DIR = BASE_DIR / ".opencode" / "agents"
AGENT_NAME = "memory-builder"
AGENT_FILE = AGENT_DIR / f"{AGENT_NAME}.md"

# Where we persist the user-editable run prompt (the per-cycle message).
PROMPTS_FILE = STATE_DIR / "prompts.json"
# Full snapshots of every saved prompt set, with a short change comment.
PROMPT_VERSIONS_FILE = STATE_DIR / "prompt_versions.json"

# --- OpenCode server (managed as a child process) ---------------------------
OPENCODE_BIN = os.environ.get("MEMORY_BUILDER_OPENCODE_BIN", "opencode")
OPENCODE_HOST = os.environ.get("MEMORY_BUILDER_OPENCODE_HOST", "127.0.0.1")
OPENCODE_PORT = int(os.environ.get("MEMORY_BUILDER_OPENCODE_PORT", "4096"))
OPENCODE_USER = "opencode"  # Basic-auth username used by `opencode serve`

# --- BaryGraph MCP ----------------------------------------------------------
# Default: the streamable-HTTP server that is already running locally.
BARYGRAPH_MCP_URL = os.environ.get("BARYGRAPH_MCP_URL", "http://127.0.0.1:8000/mcp")
# Optional: switch to a local stdio server by setting BARYGRAPH_MCP_COMMAND
# (e.g. "python3 -m scripts.mcp_server") and BARYGRAPH_MCP_CWD.
BARYGRAPH_MCP_COMMAND = os.environ.get("BARYGRAPH_MCP_COMMAND", "").strip()
BARYGRAPH_MCP_CWD = os.environ.get("BARYGRAPH_MCP_CWD", "/home/alex/Projects/bary-vector")

# --- Web app ----------------------------------------------------------------
APP_HOST = os.environ.get("MEMORY_BUILDER_HOST", "127.0.0.1")
APP_PORT = int(os.environ.get("MEMORY_BUILDER_PORT", "8765"))

# Per-cycle wall-clock timeout (seconds).
CYCLE_TIMEOUT = int(os.environ.get("MEMORY_BUILDER_CYCLE_TIMEOUT", "300"))
# Automatic retries for rate-limit / quota errors (free tiers hit these often).
CYCLE_RETRIES = int(os.environ.get("MEMORY_BUILDER_CYCLE_RETRIES", "2"))
# Abort a cycle early if the provider sends no model output at all within this
# many seconds (some free models accept the request and then hang forever).
FIRST_EVENT_TIMEOUT = int(os.environ.get("MEMORY_BUILDER_FIRST_EVENT_TIMEOUT", "300"))
# Tell the UI when a cycle has produced nothing for this long.
FIRST_EVENT_NOTICE = int(os.environ.get("MEMORY_BUILDER_FIRST_EVENT_NOTICE", "20"))

# --- Identity pilot runtime -------------------------------------------------
PILOT_DEFAULT_STEPS = int(os.environ.get("MEMORY_BUILDER_PILOT_STEPS", "12"))
PILOT_DEFAULT_TOP_K = int(os.environ.get("MEMORY_BUILDER_PILOT_TOP_K", "3"))
# Per-turn wall-clock timeout for a pilot think/extract call.
PILOT_STEP_TIMEOUT = int(os.environ.get("MEMORY_BUILDER_PILOT_STEP_TIMEOUT", "240"))
# Abort a pilot turn that accepts the request but sends no model output.
PILOT_FIRST_EVENT_TIMEOUT = int(
    os.environ.get("MEMORY_BUILDER_PILOT_FIRST_EVENT_TIMEOUT", "180")
)
PILOT_FIRST_EVENT_NOTICE = int(
    os.environ.get("MEMORY_BUILDER_PILOT_FIRST_EVENT_NOTICE", "20")
)
PILOT_TURN_RETRIES = int(os.environ.get("MEMORY_BUILDER_PILOT_TURN_RETRIES", "1"))
# Do not re-inject an identical construct for this many steps. A tight loop of
# the same memory hit makes the pilot collapse to a fixed point.
PILOT_MEMORY_EXCLUDE_WINDOW = int(
    os.environ.get("MEMORY_BUILDER_PILOT_EXCLUDE_WINDOW", "3")
)
# Ignore weak word-overlap matches. Without a floor, unrelated seeded
# constructs can be injected and copied into the next thought.
PILOT_MEMORY_MIN_SCORE = float(
    os.environ.get("MEMORY_BUILDER_PILOT_MIN_SCORE", "0.12")
)
# A single shared word is usually coincidental ("answer", "memory", "fix").
# Require at least two overlapping terms so one common token cannot pull in an
# unrelated construct from a different domain.
PILOT_MEMORY_MIN_SHARED = int(
    os.environ.get("MEMORY_BUILDER_PILOT_MIN_SHARED", "2")
)


def ensure_dirs() -> None:
    for path in (
        OUTPUT_DIR,
        RUNS_DIR,
        STATE_DIR,
        JOBS_DIR,
        AGENT_DIR,
        PILOT_DIR,
        PILOT_RUNS_DIR,
        PILOT_JOBS_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)


def is_free_or_ollama(model: dict) -> bool:
    """Filter the model catalog down to the models the UI should offer.

    Includes Ollama, anything explicitly marked free by id, and any zero-cost
    model that can actually call tools with text output (so embeddings,
    rerankers, and guard models are skipped).
    """
    provider = model.get("providerID", "")
    model_id = model.get("id", "")
    if provider == "ollama":
        return True

    caps = model.get("capabilities")
    if isinstance(caps, dict):
        if caps.get("tools") is not True:
            return False
        outputs = caps.get("output")
        if outputs and "text" not in outputs:
            return False
        inputs = caps.get("input") or []
        # Drop voice/realtime models (audio but no other chat media); keep
        # multimodal chat models that merely also accept audio.
        if "audio" in inputs and not ({"image", "video", "pdf"} & set(inputs)):
            return False

    if "free" in model_id.lower():
        return True

    costs = model.get("cost") or []
    if costs:
        return all(c.get("input", 0) == 0 and c.get("output", 0) == 0 for c in costs)
    return False
