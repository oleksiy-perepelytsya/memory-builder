# AGENTS.md

This file is for coding agents working on **Memory Builder**. It explains the
idea behind the app, how the system fits together, and the invariants that must
survive changes.

## 1. What this project is

Memory Builder is a local extraction harness. It drives free OpenCode /
TokenHarbor models (or local Ollama models) through the OpenCode HTTP API and
asks each model to produce **one structured memory record per cycle**. Records
are appended as JSONL, one line per cycle.

The model is expected to ground its record with **BaryGraph** MCP tools. BaryGraph
is an associative atlas built from Wiktionary: words, senses, relations, and
relation clusters. It is a source of *coordinates worth investigating*, not an
answer engine. A graph result is a prompt for interpretation, not proof.

The output schema is **not hard-coded in Python**. It is defined by the
user-editable **run prompt** in the prompts drawer (or `.state/prompts.json`).
The system prompt is the agent's behavior contract; the run prompt is the
per-cycle message that defines the desired JSON object.

## 2. Core product invariants

These are the rules the rest of the system is built around:

1. **N cycles = N independent runs.** Each cycle gets a fresh OpenCode session
   and appends one JSON value to the same `.jsonl` file.
2. **Append-only output.** A run file is opened once and records are appended;
   failed cycles are written as `_error` records instead of being skipped.
   Continuing an interrupted run reopens the same file in append mode, so
   existing records are never rewritten.
3. **Saved prompts only.** A run uses the current saved prompt version. Unsaved
   editor changes must be saved with a comment before Start is allowed.
4. **Every new record carries prompt provenance.** Each dict record is stamped
   with `_prompt_version`, `_prompt_revision`, and `_prompt_comment`.
5. **Live and historical results are isolated.** Historical results render in a
   separate container (`#history-results`) with a banner and badges. Live cycles
   continue in `#results` without overwriting the history view.
6. **One private OpenCode server per app process.** The app starts its own
   `opencode serve` on port 4096 (or a free port) and stops it on shutdown.
7. **Runtime data is not source.** `.state/` and `output/` are git-ignored.
   They can contain prompts, logs, model output, and job traces.

## 3. High-level architecture

```text
Browser (web/)
  │  HTML/CSS/JS, EventSource for live updates
  ▼
FastAPI app (app/main.py)
  │  REST + SSE, prompt versioning, run orchestration
  ├──▶ JobManager (app/runner.py)
  │       creates sessions, prompts the model, parses JSON,
  │       appends JSONL, writes job state
  ├──▶ OpenCodeClient (app/opencode.py)
  │       HTTP + SSE client for a private `opencode serve`
  └──▶ OpenCodeServer (app/opencode.py)
          child process, Basic auth password captured from its log

OpenCode
  └──▶ BaryGraph MCP (streamable HTTP, default http://127.0.0.1:8000/mcp)
          exposed through Code Mode as tools.barygraph.<tool>(...)
```

### Main components

| Path | Responsibility |
| --- | --- |
| `app/main.py` | FastAPI app, routes, prompt/agent persistence, prompt versions, lifespan, startup/shutdown. |
| `app/runner.py` | `Job`, `JobManager`, per-cycle orchestration, retries, timeouts, SSE event routing, JSONL writing. |
| `app/pilot.py` | `PilotJob`, `PilotManager`, identity-pilot think/extract/retrieve loop, pilot SSE events, pilot JSONL writing. |
| `app/pilot_memory.py` | Append-only pilot construct memory, word-overlap retrieval, seeding from existing runs/examples. |
| `app/pilot_prompts.py` | Separate pilot prompt store and version snapshots in `.state/pilot_prompts.json`. |
| `app/opencode.py` | Manages the private `opencode serve` child process and wraps its HTTP/SSE API. |
| `app/config.py` | Filesystem paths, environment variables, model filtering, pilot paths/timeouts. |
| `web/index.html` | Two-row top bar, prompt drawer, tabbed workspace, result panes, pilot link. |
| `web/app.js` | UI state, API calls, SSE handlers, prompt versions, auto-scroll, history isolation. |
| `web/styles.css` | Layout, sticky tab bar, drawer, panes, history banner, badges, toasts. |
| `web/pilot.html` | Separate identity-pilot page at `/pilot`. |
| `web/pilot.js` | Pilot model picker, prompt drawer, live step timeline, memory panel, pilot runs. |
| `web/pilot.css` | Standalone styling for the pilot page. |
| `.opencode/agents/memory-builder.md` | Custom agent definition. Its body is the editable system prompt. |
| `opencode.json` | Declares the BaryGraph MCP server to OpenCode. |
| `prompts-examples/default.md` | Example prompt material; not runtime state. |

## 4. Startup sequence

`app.main.lifespan` does the following:

1. `config.ensure_dirs()` — creates `output/`, `output/runs/`, `.state/`,
   `.state/jobs/`, and `.opencode/agents/`.
2. `migrate_legacy_runs()` — moves old top-level `output/*.jsonl` files into
   `output/runs/`.
3. `write_agent(read_agent_system() or DEFAULT_SYSTEM_PROMPT)` — normalizes the
   agent file and its frontmatter.
4. `ensure_prompt_version()` — creates or validates `.state/prompt_versions.json`.
5. `pilot_prompts.ensure_current_snapshot()` — validates the separate pilot
   prompt store.
6. `pilot_memory.seed_from_existing()` — imports construct-shaped records from
   examples and existing runs into `output/pilot/constructs.jsonl` when missing.
7. `OpenCodeServer.start()` — starts `opencode serve`, captures the Basic-auth
   password from `.state/opencode-serve.log`, and waits for health.
8. Waits up to ~30s for the `barygraph` MCP server to report `connected` so the
   first run has tools available.
9. Stores `server`, `client`, `jobs`, and `pilots` on `app.state`.

On shutdown it closes the HTTP client and terminates the OpenCode child process.

## 5. Run lifecycle

### Starting a run

`POST /api/run` requires a model selection and uses the **saved** prompt set.
The UI no longer sends prompt text on Start; if an old client sends prompt text
that differs from the saved prompt, the backend returns `409` and tells the
caller to save a version first. A stale `base_revision` also returns `409`.

`JobManager.start()` creates a `Job` and launches `_run()` as an asyncio task.
The job is immediately available to the UI through SSE.

### Per cycle

For each cycle:

1. `_run_cycle()` emits `cycle.started`.
2. `_attempt()` creates a new OpenCode session with the selected agent/model.
3. It posts the saved run prompt to that session.
4. `_listen()` routes OpenCode events for that session into the job SSE queue.
5. Timeouts:
   - `MEMORY_BUILDER_CYCLE_TIMEOUT` is the total cycle wall-clock limit.
   - `MEMORY_BUILDER_FIRST_EVENT_TIMEOUT` aborts models that accept the request
     but never emit anything.
   - `MEMORY_BUILDER_CYCLE_RETRIES` retries rate-limit/quota failures with a
     delay derived from the provider message.
6. After completion, `_assistant_extract()` collects the final text, reasoning,
   and tool calls.
7. `parse_json_object()` extracts the first parseable JSON object. It tolerates
   markdown fences and surrounding prose.
8. The record is written to the run file and the SSE queue gets
   `cycle.completed`.

### Failure records

Failures are never silently dropped. They are written as dict records such as:

```json
{"_error": "timeout", "_message": "...", "_cycle": 2, "_raw": "..."}
{"_error": "unparsed_output", "_raw": "...", "_cycle": 1}
{"_error": "provider_error", "_message": "...", "_status": 429, "_cycle": 3}
```

Dict records (including error records) also receive the prompt provenance
fields. A non-dict model output cannot carry object fields; that edge case is
accepted.

### Continuing an interrupted run

Each job also writes a small manifest to `.state/run_meta/<run-stem>.json`
(planned `cycles`, `start_cycle`, completed count, model, status). The
**Previous runs** list exposes `POST /api/runs/{name}/continue` as a
**Continue** button when a run has fewer records than its planned cycle count,
or when the target is unknown and its last record is an `_error`.

Continuing reopens the same `.jsonl` in append mode and starts a new job at
`start_cycle = records + 1`, so earlier records are untouched and the file keeps
its name. The job uses the saved prompt set and stamps new records with the
current prompt version. `backfill_run_meta()` creates manifests for runs that
predate this feature from their `.state/jobs/<job>.json` entries.

### Live events

OpenCode SSE events are translated into app-level events:

| App event | Meaning |
| --- | --- |
| `job.started` | Job accepted and the worker is running. |
| `cycle.started` | A new cycle/session began. |
| `reasoning.delta` | Incremental reasoning text. |
| `text.delta` | Incremental final-output text. |
| `tool.call` | A tool call started; Code Mode calls are labeled `barygraph: ...`. |
| `tool.result` | A tool call succeeded or failed. |
| `cycle.waiting` | Provider has emitted nothing yet; UI hint. |
| `cycle.retry` | Rate-limit retry scheduled. |
| `cycle.completed` | One record finished; includes parsed object, raw text, reasoning, tools. |
| `job.completed` | Job finished, stopped, or failed; includes output path and prompt version. |

The UI consumes these with `EventSource` in `web/app.js`.

### Reattaching a reloaded page

A reload closes the `EventSource` but does not stop the server-side job. The
page reattaches on boot: it asks `GET /api/run/active` for the newest running
job, and falls back to the job id remembered in `sessionStorage` for backends
that predate that endpoint. Reattachment replays the job's completed results
into `#live` and subscribes to the same SSE stream. The stored id is cleared
when `job.completed` arrives. A reload cannot revive a job the server no longer
has (for example after an app restart); that run file stays in **Previous
runs**.

## 6. Prompt and agent persistence

There are two prompts:

- **System prompt** — stored in `.opencode/agents/memory-builder.md` as the body
  of the custom agent. The app owns this file and writes YAML frontmatter around
  the body:
  ```yaml
  ---
  description: Memory Builder: extracts structured JSONL records via BaryGraph
  mode: primary
  permissions:
    - action: "edit"
      resource: "*"
      effect: deny
    - action: "shell"
      resource: "*"
      effect: deny
  ---
  ```
  The agent is intentionally not allowed to edit files or run shell commands.
- **Run prompt** — stored in `.state/prompts.json` under `run_prompt`, together
  with the last selected model/effort.

### Prompt versioning

Every saved prompt change creates a full snapshot in
`.state/prompt_versions.json`:

```json
{
  "current": 4,
  "versions": [
    {
      "version": 4,
      "created": "2026-11-06T20:50:00+00:00",
      "comment": "Added optional tension with null",
      "revision": "c300a917498c2f0a",
      "system_prompt": "...",
      "run_prompt": "..."
    }
  ]
}
```

Rules:

- The file is created lazily with `v1 — Initial version` if absent.
- Saving changed prompts requires a non-empty `prompt_comment`.
- Saving unchanged prompts does not create a new version.
- If prompts are edited outside the UI, `ensure_prompt_version()` snapshots the
  live content as a `Detected external prompt edit` version.
- `prompt_revision()` is a short SHA-256 of the system + run prompt and is used
  for stale-tab guards.
- Selecting an older version loads its prompts into the editors. Saving that
  content with a new comment creates a new current version; history is never
  rewritten.
- Runs read the saved prompt set, then stamp each record with the exact version
  used.

The UI keeps `promptBaseline` from the last load/save. If the editors differ,
Start is blocked and the drawer opens with the comment field focused.

## 7. HTTP API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | Serve the UI. |
| `GET` | `/api/health` | App/OpenCode health. |
| `GET` | `/api/models` | Free/Ollama model catalog, filtered by `config.is_free_or_ollama`. |
| `GET` | `/api/config` | Agent presence, prompts, revision, prompt version list. |
| `POST` | `/api/config/prompts` | Save prompts; requires `base_revision` and, if changed, `prompt_comment`. |
| `GET` | `/api/config/prompts/versions/{version}` | Full snapshot of one prompt version. |
| `GET` | `/api/barygraph` | MCP server status from OpenCode. |
| `POST` | `/api/run` | Start a job from the saved prompt set. |
| `GET` | `/api/run/active` | Newest running job, used to reattach after a page reload. |
| `GET` | `/api/run/{job_id}/events` | SSE stream for one job. |
| `POST` | `/api/run/{job_id}/stop` | Request a stop and abort the current session. |
| `GET` | `/api/run/{job_id}` | Job summary plus completed results. |
| `GET` | `/api/runs` | List files in `output/runs/` with record counts and resumability. |
| `GET` | `/api/runs/{name}` | Parse and return one run file. |
| `POST` | `/api/runs/{name}/continue` | Append the remaining cycles to an interrupted run. |
| `GET` | `/api/runs/{name}/download` | Download a run file. |
| `DELETE` | `/api/runs/{name}` | Delete a run file. |
| `GET` | `/api/examples` | List curated examples. |
| `GET` | `/api/examples/download` | Download `output/examples.jsonl`. |
| `POST` | `/api/examples` | Add a record to examples (deduplicated by canonical JSON). |
| `DELETE` | `/api/examples/{index}` | Delete an example by index. |

Identity-pilot routes are separate:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/pilot` | Pilot page. |
| `GET` | `/api/pilot/config` | Pilot prompts, versions, preferences, memory stats. |
| `POST` | `/api/pilot/prompts` | Save pilot prompts with revision guard/comment. |
| `GET` | `/api/pilot/prompts/versions/{version}` | One pilot prompt snapshot. |
| `POST` | `/api/pilot/run` | Start a pilot trajectory. |
| `GET` | `/api/pilot/run/{job_id}/events` | Pilot SSE stream. |
| `POST` | `/api/pilot/run/{job_id}/stop` | Stop the current pilot turn/job. |
| `GET` | `/api/pilot/run/{job_id}` | Pilot summary + steps + result. |
| `GET` | `/api/pilot/memory` | List/filter construct memory. |
| `POST` | `/api/pilot/memory/seed` | Import constructs from examples/runs. |
| `DELETE` | `/api/pilot/memory` | Clear construct memory. |
| `GET` | `/api/pilot/memory/download` | Download memory JSONL. |
| `GET` | `/api/pilot/runs` | List pilot result files. |
| `GET` | `/api/pilot/runs/{name}` | Parse one pilot result file. |
| `GET` | `/api/pilot/runs/{name}/download` | Download a pilot result file. |
| `DELETE` | `/api/pilot/runs/{name}` | Delete a pilot result file. |

All prompt and run payloads are JSON. The UI uses a small `api()` helper that
turns non-2xx responses into errors with status codes.

## 8. Storage layout

```text
.state/
  prompts.json               current run prompt + last model
  prompt_versions.json       full prompt snapshots + comments
  pilot_prompts.json         pilot prompts + separate version snapshots
  pilot_jobs/<job>.json      pilot summary + step trajectory/result
  opencode-serve.log         private OpenCode server log/password
  jobs/<job>.json            job summary + per-cycle reasoning/tools/results
  run_meta/<run>.json        lightweight manifest for resuming an interrupted run
output/
  examples.jsonl             curated examples
  runs/<timestamp>.jsonl     generated run records
  pilot/runs/<timestamp>.jsonl   identity-pilot trajectory records
  pilot/constructs.jsonl     append-only pilot construct memory
```

`.state/` and `output/` are ignored by git. Do not commit them. `.state/` can
contain prompts, logs, and full model traces; treat it as local data.

## 9. BaryGraph integration

`opencode.json` declares:

```json
{
  "mcp": {
    "servers": {
      "barygraph": {
        "type": "remote",
        "url": "http://127.0.0.1:8000/mcp",
        "oauth": false
      }
    }
  }
}
```

Important details:

- BaryGraph is used through OpenCode **Code Mode**. The agent must call
  `tools.barygraph.<tool>(...)` inside the `execute` tool. Native
  `barygraph_*` tool names do not exist and fail.
- The app waits for the MCP server to report `connected` at startup, but a
  briefly unavailable server can still make the first cycle tool-less.
- `_tool_label()` turns Code Mode `execute` calls into readable labels such as
  `barygraph: semantic_search` for the live UI.
- Do not set `"codemode": false` for this server: it creates conflicting tool
  protocols and weak models often call nonexistent native tools.
- If the live BaryGraph server exposes fewer than 12 tools (older process),
  restart it from the BaryGraph repo:
  ```sh
  cd /home/alex/Projects/bary-vector
  scripts/mcp_server_ctl.sh restart
  ```
- BaryGraph results are noisy by design. Treat them as investigative prompts,
  not facts. L10/L11 ancestry in particular can contain unrelated residue.

The app can also use a local stdio MCP server via `BARYGRAPH_MCP_COMMAND` and
`BARYGRAPH_MCP_CWD`; the default working directory is
`/home/alex/Projects/bary-vector`.

## 10. Models and free-tier behavior

`config.is_free_or_ollama()` keeps:

- all `ollama` models;
- models whose id contains `free`;
- zero-cost models that support tools and text output, excluding embeddings,
  rerankers, guard models, and voice-only models.

Known operational issues on this machine:

- `nvidia/deepseek-ai/deepseek-v4.1-flash` and `z-ai/glm-5.3-flash` can hang
  upstream. `nvidia/nemotron-3-ultra-550b-a55b` and `minimax-m3.1-flash` have
  worked better.
- Google free-tier input limits (16k tokens/min) cause `429` quota errors on
  multi-cycle runs; the retry path handles some of these but not all.

## 11. Frontend conventions

- Layout: two top-bar rows, a left prompt drawer, and one tabbed workspace with
  **Live**, **Results**, **Previous runs**, and **Examples**.
- The tab bar is sticky. Its offset is measured from the actual top-bar height
  via `--topbar-h` and a `ResizeObserver`.
- Text panes (`thinking`, output, tools) scroll internally. Auto-follow uses a
  24px near-bottom threshold; if the user scrolls up, new events must not yank
  the view back down.
- Live results (`#results`) and historical results (`#history-results`) are
  separate DOM containers. Opening a previous run shows a history banner and an
  amber `history` badge; live results keep a blue `new` badge.
- The prompt drawer shows the version selector with date + comment. Current
  version is marked `●`; selecting an older version displays "Viewing vN".
- Toasts are used for save/run/load feedback.
- UI assets are cache-busted with `?v=16`. After changing `web/app.js` or
  `web/styles.css`, bump the version in `web/index.html` so a stale tab cannot
  keep old behavior.
- Reloading the page must reattach to a running job: boot asks
  `/api/run/active`, falls back to the `sessionStorage` job id, replays the
  completed results, and subscribes to the same SSE stream.
- The identity pilot is a separate page (`/pilot`) with its own
  `pilot.html` / `pilot.js` / `pilot.css` and its own cache-busting versions. It
  must not import main-page state assumptions or alter `#results` /
  `#history-results`.
- Pilot live steps use `step.started`, `step.thought`, `step.constructs`,
  `step.memory`, and `step.completed`. Do not collapse them into the main
  cycle event names.
- Pilot step thoughts are folded by default; the final-answer card opens the
  last thought. Previous pilot runs are listed by the question (truncated to
  ~50 chars), with the filename kept as a secondary line.
- Preserve the existing plain HTML/CSS/JS approach. There is no frontend build
  step or framework.

## 12. Local development

```sh
cd /home/alex/Projects/memory-builder
./run.sh
# open http://127.0.0.1:8765
```

The app starts its own OpenCode server on port 4096 (or a free port if 4096 is
busy). It writes logs to `.state/opencode-serve.log`; app logs go wherever
`run.sh` was redirected.

Useful checks:

```sh
curl -s http://127.0.0.1:8765/api/health
curl -s http://127.0.0.1:8765/api/config
python3 -m py_compile app/*.py
node --check web/app.js
```

Restarting:

1. Find and stop the process listening on 8765 and the private OpenCode process
   on 4096.
2. Start `./run.sh` again.
3. Wait for `/api/health`.

Avoid broad `pkill -f "uvicorn app.main"` patterns; they can kill unrelated
processes. Prefer the exact PIDs from `ss -ltnp`.

## 13. Change checklist for coding agents

Before finishing a change:

- Preserve the one-JSON-value-per-line contract and append-only run files.
- Do not reintroduce prompt text into Start payloads; prompt changes must be
  saved as a version first.
- Keep the system prompt / run prompt distinction intact.
- Keep prompt provenance on new records and job state.
- Preserve live/historical result isolation and auto-scroll behavior.
- Keep pilot output and pilot memory separate from normal run output.
- Bump the UI asset version after changing JS/CSS.
- Run `python3 -m py_compile app/*.py`, `node --check web/app.js`, and
  `node --check web/pilot.js`.
- Exercise affected API endpoints with `curl` or the UI.
- Update `README.md` for user-visible behavior changes.
- Do not commit `.state/`, `output/`, `__pycache__/`, `.venv/`, or `.env`.

## 14. Known limitations

- There is no automated test suite; verification is usually API calls,
  headless-browser checks, and a small mocked `JobManager` cycle.
- Prompt history is full snapshots, not diffs, so it grows with every save.
- If a model returns a non-dict JSON value, the record cannot be stamped with
  prompt provenance.
- The app is local-first: no authentication beyond the private OpenCode server's
  Basic auth, no multi-user isolation, and no database.
- BaryGraph availability and free-model reliability are external dependencies;
  failures are surfaced as `_error` records rather than hidden.
- Pilot memory retrieval is word overlap, not embeddings; near-synonyms are
  missed unless they share a normalized token. The memory file can contain
  imported BaryGraph constructs from older runs, so hits may be noisy.
- Pilot memory has a short novelty window (`PILOT_MEMORY_EXCLUDE_WINDOW`) so a
  just-used triad is not immediately re-injected. Without it, high-scoring
  constructs can collapse the trajectory into a fixed point. Retrieval also
  applies `PILOT_MEMORY_MIN_SCORE` and `PILOT_MEMORY_MIN_SHARED`, and returns at
  most one hit per source run. The novelty key is the triad's term sets, so
  changing only the tension does not bypass the window.
- Pilot think steps use the same agent/system prompt as the normal extractor;
  stage prompts explicitly ask for prose, but a very rigid model can still try
  to emit JSON early.
