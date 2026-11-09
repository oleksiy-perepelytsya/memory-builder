# Memory Builder

A local web app that drives **OpenCode** models to produce **JSONL memory
records**, optionally grounded with the **BaryGraph** MCP tools.

- Pick any free OpenCode / TokenHarbor model or a local **Ollama** model.
- Edit the **system prompt** (overrides the agent's default prompt) and the
  **run prompt** (the per-cycle instruction, where you define your JSONL spec).
- Choose an **effort / variant** (maps to the model's `reasoningEffort`).
- Run **N cycles**; each cycle is a fresh session that appends one JSON object
  as a line to a single `.jsonl` file.
- Watch the **thinking**, live tool calls, and output; browse past runs.

## Identity Pilot

`/pilot` is a separate experimental page for a memory-driven thought trajectory.
It does not change the normal Memory Builder run flow.

For each pilot step the app:

1. asks the model to continue an open-ended thought in prose;
2. asks the model to extract 1–2 triad constructs from that thought;
3. searches the accumulated construct memory by word overlap;
4. injects the best matches into the next step as the model's own prior memory;
5. repeats for the configured number of steps.

Pilot constructs are stored in `output/pilot/constructs.jsonl`. On first
startup the app seeds that file from construct-shaped records already present in
`output/examples.jsonl` and `output/runs/*.jsonl`, so the pilot does not start
from a completely empty memory. Pilot results go to `output/pilot/runs/` and
pilot prompt versions live in `.state/pilot_prompts.json`.

The pilot maintains a short novelty window: a triad that was just produced or
retrieved is not re-injected for the next few steps. Without that guard the
same high-scoring construct can become a self-reinforcing fixed point. Retrieval
also drops weak matches below a score floor and keeps at most one hit per source
run, so a single noisy run cannot fill every memory slot.

The pilot is intentionally simple: retrieval is word overlap, not embeddings or
BaryGraph vector search. Treat it as a test harness for trajectory changes and
construct memory, not as a finished identity model.

On the page, step thoughts are folded by default so a long trajectory stays
scannable; the final answer card opens the last thought. Previous pilot runs are
listed by the first ~50 characters of their question instead of the filename.

## Architecture

```
Browser (web/)  ──HTTP/SSE──▶  FastAPI (app/main.py)  ──HTTP──▶  opencode serve
                                      │                              │
                                      │                              └── BaryGraph MCP (streamable HTTP)
                                      └── .opencode/agents/memory-builder.md  (editable system prompt)
                                      └── output/runs/<timestamp>.jsonl       (results)
```

The app starts its **own** private `opencode serve` process (default port 4096,
auto-picks a free port if busy) so it never disturbs your shared background
service. It authenticates with the Basic-auth password the server prints on
startup.

## Requirements

- `opencode` on `PATH` (v2.x)
- Python 3.10+ with `fastapi`, `uvicorn`, `httpx` (already present here)
- A running BaryGraph MCP server (default `http://127.0.0.1:8000/mcp`)
- Models: free OpenCode/TokenHarbor models come from your OpenCode config;
  Ollama models come from the `ollama` provider (base URL `http://127.0.0.1:11435/v1`)

## Run

```sh
./run.sh
# then open http://127.0.0.1:8765
```

## Docker

The app runs in a container with its own private OpenCode server. The
container uses host networking, so it can reach a BaryGraph running on the
Docker host (including `127.0.0.1:8000`) without firewall changes, and the
NVIDIA key is passed as an environment variable.

### Quick start

```sh
cp .env.example .env   # set NVIDIA_API_KEY
docker compose up -d --build
# open http://127.0.0.1:8765
```

Stop a local `./run.sh` first if it still holds port `8765`.

State is kept under `./docker-data/` on the host:

| Path | Contents |
| --- | --- |
| `docker-data/state/` | prompts, prompt versions, job metadata, OpenCode log |
| `docker-data/output/` | generated runs, examples, and pilot data |
| volume `memory-builder-agents` | `memory-builder.md` system prompt (seeded from the image) |

### Networking and BaryGraph

Host networking is the default because the host's BaryGraph endpoint
(`BARYGRAPH_MCP_URL=http://127.0.0.1:8000/mcp`) then works as-is. The
entrypoint renders `BARYGRAPH_MCP_URL` into the container's `opencode.json` at
startup. The web UI binds `0.0.0.0:8765` by default; set
`MEMORY_BUILDER_HOST=127.0.0.1` in `.env` to keep it loopback-only.

If BaryGraph runs in another container or on another machine, keep host
networking and point `BARYGRAPH_MCP_URL` at its address (for example
`http://10.0.0.5:8000/mcp`).

Bridge networking also works (for Docker Desktop or network isolation): set
`BARYGRAPH_MCP_URL=http://host.docker.internal:8000/mcp`, add
`extra_hosts: ["host.docker.internal:host-gateway"]`, and publish port
`8765:8765`. On Linux, UFW blocks containers from reaching host services by
default, so allow the Docker subnet first:

```sh
sudo ufw allow from 172.16.0.0/12 to any port 8000 proto tcp
```

### NVIDIA API key

OpenCode's built-in `nvidia` provider reads `NVIDIA_API_KEY` from the
environment, so the app offers the NVIDIA models listed by the catalog. Keep
the key in `.env`; it is not baked into the image or committed.

### Multiple loops with separate keys

`docker-compose.multi.yml` starts two independent instances, each with its own
key, state directory, and web port:

```sh
# .env
NVIDIA_API_KEY_1=nvapi-...
NVIDIA_API_KEY_2=nvapi-...
MEMORY_BUILDER_WEB_PORT_1=8765
MEMORY_BUILDER_WEB_PORT_2=8766
```

```sh
docker compose -f docker-compose.multi.yml up -d --build
```

Both instances connect to the same BaryGraph but keep separate OpenCode
sessions, prompts, memories, and outputs. Copy a service block to add more.

### Notes

- Pin the OpenCode version with `--build-arg OPENCODE_VERSION=2.1.2` when a
  reproducible image matters.
- The container runs as root and writes to the bind mounts as root.
- Set `MEMORY_BUILDER_WEB_PORT` to move the host port without editing the
  compose file.

## Configuration

Environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `MEMORY_BUILDER_PORT` | `8765` | Web app port |
| `MEMORY_BUILDER_OPENCODE_PORT` | `4096` | Preferred `opencode serve` port |
| `BARYGRAPH_MCP_URL` | `http://127.0.0.1:8000/mcp` | Remote BaryGraph MCP endpoint |
| `BARYGRAPH_MCP_COMMAND` | – | Set to use a local stdio MCP server instead |
| `BARYGRAPH_MCP_CWD` | `/home/alex/Projects/bary-vector` | Working dir for the local stdio server |
| `MEMORY_BUILDER_CYCLE_TIMEOUT` | `300` | Per-cycle timeout (seconds) |
| `MEMORY_BUILDER_CYCLE_RETRIES` | `2` | Automatic retries on rate-limit/quota errors |
| `MEMORY_BUILDER_FIRST_EVENT_TIMEOUT` | `300` | Fail a cycle early if the provider sends nothing at all |
| `MEMORY_BUILDER_FIRST_EVENT_NOTICE` | `20` | Show a "waiting for provider" hint after this many seconds |
| `MEMORY_BUILDER_PILOT_STEPS` | `12` | Default number of identity-pilot steps |
| `MEMORY_BUILDER_PILOT_TOP_K` | `3` | Default number of memory hits injected per pilot step |
| `MEMORY_BUILDER_PILOT_STEP_TIMEOUT` | `240` | Per-turn timeout for a pilot think/extract call |
| `MEMORY_BUILDER_PILOT_FIRST_EVENT_TIMEOUT` | `180` | Fail a pilot turn early if the provider sends nothing |
| `MEMORY_BUILDER_PILOT_TURN_RETRIES` | `1` | Retries for rate-limited pilot turns |
| `MEMORY_BUILDER_PILOT_EXCLUDE_WINDOW` | `3` | Steps before a used triad can be re-injected |
| `MEMORY_BUILDER_PILOT_MIN_SCORE` | `0.12` | Minimum word-overlap score for a memory hit |
| `MEMORY_BUILDER_PILOT_MIN_SHARED` | `2` | Minimum shared terms before a memory hit is injected |
| `MEMORY_BUILDER_LOG` | `INFO` | `DEBUG` for event-level logs |

The BaryGraph MCP server is declared in `opencode.json`. To switch to the local
stdio server, replace the `barygraph` entry with:

```json
"barygraph": { "type": "local", "command": ["python3", "-m", "scripts.mcp_server"], "cwd": "/home/alex/Projects/bary-vector" }
```

## Tool mode (Code Mode)

BaryGraph uses OpenCode's **Code Mode** (the default; `codemode` is not set to
`false`). The agent calls it through the `execute` tool with the exact paths
from the Code Mode catalog:

```js
const seed = await tools.barygraph.sample_metabary({ level: 12, n: 1, with_parent: true });
return seed;
```

Do **not** set `"codemode": false` for this server. That exposes the tools on
the provider's native tool list while OpenCode still injects Code Mode
instructions, so weak models see two conflicting protocols and often call
`tools.barygraph_sample_metabary`, which fails with `Unknown tool`.

The agent prompt expects the current BaryGraph server, which includes
`enrich_request` and `semantic_search`. If the live server is an older process
(the `tools/list` response has only 9 tools), restart it from the BaryGraph
repo:

```sh
cd /home/alex/Projects/bary-vector
scripts/mcp_server_ctl.sh restart
```

With `MCP_PUBLIC=1` it exposes the 12 read-only tools; the write tools stay
hidden.

## Files

- `app/` – FastAPI backend (`main.py`, `opencode.py`, `runner.py`, `pilot.py`,
  `pilot_memory.py`, `pilot_prompts.py`, `config.py`)
- `web/` – static UI (`index.html`, `app.js`, `styles.css`, plus the separate
  `pilot.html`, `pilot.js`, `pilot.css` identity-pilot page)
- `AGENTS.md` – project guide for coding agents: idea, architecture, invariants,
  and safe change workflow
- `.opencode/agents/memory-builder.md` – the custom agent; its body is the
  editable system prompt (written by the app)
- `.state/` – run metadata, job records, prompts/settings, prompt version
  history (`prompt_versions.json`), pilot prompts (`pilot_prompts.json`),
  server log
- `output/runs/` – generated JSONL result files (deletable from the UI)
- `output/examples.jsonl` – curated records you marked with ☆ Example
- `output/pilot/runs/` – identity-pilot trajectory records
- `output/pilot/constructs.jsonl` – accumulated pilot construct memory

## Output

Each cycle appends one line. Records that fail to parse are written as
`{"_error": "...", "_raw": "...", "_cycle": N}` so nothing is silently lost.
Provider failures (e.g. a 429 quota error) are written as
`{"_error": "<type>", "_message": "...", "_status": N, "_cycle": N}` and shown
in the UI. Rate-limit/quota errors are retried automatically (see
`MEMORY_BUILDER_CYCLE_RETRIES`). A model that never responds is aborted after
`MEMORY_BUILDER_FIRST_EVENT_TIMEOUT` and recorded as `no_response`. Per-cycle
reasoning and tool calls are kept in `.state/jobs/<job>.json`.

The last used model and effort (variant) are persisted in `.state/prompts.json`
and restored in the UI on the next visit.

## Prompt versions

When prompts change, save them with a short version comment. The app snapshots
the system prompt and run prompt in `.state/prompt_versions.json` and shows the
comments in the prompts drawer's **Prompt version** selector. Selecting an
older version loads it into the editors; save it with a new comment to make it
current again. Runs always use the saved current prompt set.

Every output record is stamped with the prompt version that produced it:

```json
{"_prompt_version": 4, "_prompt_revision": "c300a917498c2f0a", "_prompt_comment": "Added optional tension with null"}
```

## Curating output

- Generated runs go to `output/runs/`. Open one from **Previous runs**, and use
  the **Delete** button there to remove a file.
- On any parsed record, click **☆ Example** to append it to
  `output/examples.jsonl`. The **Examples** panel lists curated records and lets
  you remove them individually.
- On startup, legacy `output/*.jsonl` files are moved into `output/runs/`
  automatically (`examples.jsonl` is left in place).
