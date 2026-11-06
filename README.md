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

- `app/` – FastAPI backend (`main.py`, `opencode.py`, `runner.py`, `config.py`)
- `web/` – static UI (`index.html`, `app.js`, `styles.css`)
- `AGENTS.md` – project guide for coding agents: idea, architecture, invariants,
  and safe change workflow
- `.opencode/agents/memory-builder.md` – the custom agent; its body is the
  editable system prompt (written by the app)
- `.state/` – run metadata, job records, prompts/settings, prompt version
  history (`prompt_versions.json`), server log
- `output/runs/` – generated JSONL result files (deletable from the UI)
- `output/examples.jsonl` – curated records you marked with ☆ Example

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
