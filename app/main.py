"""FastAPI application: model picker, prompt editing, run orchestration, SSE."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config
from .opencode import OpenCodeClient, OpenCodeServer
from .runner import JobManager, new_output_path, parse_json_object

logging.basicConfig(
    level=os.environ.get("MEMORY_BUILDER_LOG", "INFO").upper(),
    format="%(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("memory_builder")

DEFAULT_SYSTEM_PROMPT = """\
You are the Memory Builder extraction agent.

You turn unstructured input into structured memory records. Use the BaryGraph
MCP tools to ground every record in the knowledge graph: search for the
concepts involved before you commit to a record.

BaryGraph is exposed through Code Mode. Call it through the `execute` tool,
using the exact `tools.barygraph.<tool>(...)` paths from the Code Mode catalog.
Do not call `barygraph_*` as native functions; those names do not exist.

Rules:
- Produce exactly ONE JSON object per cycle.
- Output raw JSON only: no prose, no markdown fences, no commentary.
- If a field is unknown, use null rather than inventing data.
- Follow the record specification defined in the run prompt exactly.
"""

DEFAULT_RUN_PROMPT = """\
Produce one memory record as a single JSON object with this shape:

{
  "id": "<short stable slug>",
  "title": "<short human title>",
  "summary": "<one paragraph>",
  "concepts": [
    {
      "term": "<word or phrase>",
      "sense_id": "<BaryGraph sense id or null>",
      "note": "<why it matters>"
    }
  ],
  "relations": [
    {
      "from": "<term>",
      "to": "<term>",
      "type": "<relation label>",
      "tension": "<optional>"
    }
  ],
  "sources": ["<source reference>"]
}

Use the BaryGraph tools to resolve terms to sense ids where possible.
Return ONLY the JSON object.
"""


# --- prompt/agent persistence ----------------------------------------------
def _parse_agent_file(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    front = parts[1]
    body = parts[2].lstrip("\n")
    meta: dict[str, Any] = {}
    for line in front.splitlines():
        if ":" in line and not line.startswith((" ", "\t")):
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta, body


AGENT_TEMPLATE = """\
---
description: {description}
mode: primary
permissions:
  - action: "edit"
    resource: "*"
    effect: deny
  - action: "shell"
    resource: "*"
    effect: deny
---

{system_prompt}
"""


def write_agent(system_prompt: str) -> None:
    config.AGENT_DIR.mkdir(parents=True, exist_ok=True)
    config.AGENT_FILE.write_text(
        AGENT_TEMPLATE.format(
            description="Memory builder: extracts structured JSONL records via BaryGraph",
            system_prompt=system_prompt.strip() + "\n",
        ),
        encoding="utf-8",
    )


def read_agent_system() -> str:
    if not config.AGENT_FILE.exists():
        write_agent(DEFAULT_SYSTEM_PROMPT)
        return DEFAULT_SYSTEM_PROMPT
    _, body = _parse_agent_file(config.AGENT_FILE.read_text(encoding="utf-8"))
    return body.strip()


def load_settings() -> dict:
    """Read persisted settings (run prompt + last used model/effort)."""
    if config.PROMPTS_FILE.exists():
        try:
            data = json.loads(config.PROMPTS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def save_settings(**updates: Any) -> None:
    data = load_settings()
    data.update(updates)
    config.PROMPTS_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def load_run_prompt() -> str:
    value = load_settings().get("run_prompt")
    return value if value is not None else DEFAULT_RUN_PROMPT


def save_run_prompt(run_prompt: str) -> None:
    save_settings(run_prompt=run_prompt)


def _prompt_hash(system_prompt: str, run_prompt: str) -> str:
    """Short hash of a prompt set, used to detect stale editor tabs."""
    h = hashlib.sha256()
    h.update(system_prompt.encode("utf-8"))
    h.update(b"\x00")
    h.update(run_prompt.encode("utf-8"))
    return h.hexdigest()[:16]


def prompt_revision() -> str:
    return _prompt_hash(read_agent_system(), load_run_prompt())


# --- prompt version history -------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_prompt_store(store: dict) -> None:
    config.PROMPT_VERSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.PROMPT_VERSIONS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, config.PROMPT_VERSIONS_FILE)


def _current_prompt_version(store: dict) -> dict:
    versions = store.get("versions") or []
    current = next(
        (v for v in versions if v.get("version") == store.get("current")),
        None,
    )
    return current or versions[-1]


def _append_prompt_version(
    store: dict, system_prompt: str, run_prompt: str, comment: str
) -> dict:
    versions = store.setdefault("versions", [])
    version = max((v.get("version", 0) for v in versions), default=0) + 1
    record = {
        "version": version,
        "created": _now_iso(),
        "comment": comment.strip(),
        "revision": _prompt_hash(system_prompt, run_prompt),
        "system_prompt": system_prompt,
        "run_prompt": run_prompt,
    }
    versions.append(record)
    store["current"] = version
    return record


def load_prompt_store() -> dict:
    """Load the version history, creating v1 from the current prompts if needed."""
    if config.PROMPT_VERSIONS_FILE.exists():
        try:
            data = json.loads(
                config.PROMPT_VERSIONS_FILE.read_text(encoding="utf-8")
            )
            if isinstance(data, dict) and isinstance(data.get("versions"), list):
                versions = [
                    v
                    for v in data["versions"]
                    if isinstance(v, dict) and isinstance(v.get("version"), int)
                ]
                if versions:
                    store = {
                        "current": data.get("current") or versions[-1]["version"],
                        "versions": versions,
                    }
                    if not any(
                        v["version"] == store["current"] for v in versions
                    ):
                        store["current"] = versions[-1]["version"]
                    return store
        except (OSError, json.JSONDecodeError):
            log.warning("could not read prompt version history")
    system_prompt = read_agent_system()
    run_prompt = load_run_prompt()
    store = {"current": 1, "versions": []}
    _append_prompt_version(
        store, system_prompt, run_prompt, "Initial version"
    )
    _write_prompt_store(store)
    return store


def ensure_prompt_version() -> tuple[dict, dict]:
    """Return the current version, snapshotting external prompt edits if needed."""
    store = load_prompt_store()
    current = _current_prompt_version(store)
    system_prompt = read_agent_system()
    run_prompt = load_run_prompt()
    revision = _prompt_hash(system_prompt, run_prompt)
    if (
        current.get("revision") != revision
        or current.get("system_prompt") != system_prompt
        or current.get("run_prompt") != run_prompt
    ):
        current = _append_prompt_version(
            store,
            system_prompt,
            run_prompt,
            "Detected external prompt edit",
        )
        _write_prompt_store(store)
    return current, store


def prompt_version_summaries(store: dict) -> list[dict]:
    return [
        {
            "version": v["version"],
            "created": v.get("created", ""),
            "comment": v.get("comment", ""),
            "current": v["version"] == store.get("current"),
        }
        for v in sorted(store.get("versions", []), key=lambda x: x["version"])
    ]


def migrate_legacy_runs() -> None:
    """Move old top-level output/*.jsonl files into output/runs/."""
    for path in list(config.OUTPUT_DIR.glob("*.jsonl")):
        if path == config.EXAMPLES_FILE:
            continue
        target = config.RUNS_DIR / path.name
        n = 1
        while target.exists():
            n += 1
            target = config.RUNS_DIR / f"{path.stem}-{n}{path.suffix}"
        try:
            path.rename(target)
        except OSError as exc:
            log.warning("could not migrate %s: %s", path, exc)


def read_examples() -> list[dict]:
    if not config.EXAMPLES_FILE.exists():
        return []
    records: list[dict] = []
    for line in config.EXAMPLES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


# --- app lifecycle ----------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    migrate_legacy_runs()
    write_agent(read_agent_system() or DEFAULT_SYSTEM_PROMPT)
    ensure_prompt_version()
    server = OpenCodeServer()
    await server.start()
    client = OpenCodeClient(server)
    app.state.server = server
    app.state.client = client
    app.state.jobs = JobManager(client)
    # Give BaryGraph a moment to connect so the first run already has its tools.
    for _ in range(30):
        try:
            servers = await client.list_mcp(str(config.BASE_DIR))
            if any(
                s.get("name") == "barygraph"
                and (s.get("status") or {}).get("status") == "connected"
                for s in servers
            ):
                break
        except Exception:  # noqa: BLE001 - best effort, do not block startup
            pass
        await asyncio.sleep(1)
    log.info("memory-builder ready")
    try:
        yield
    finally:
        await client.aclose()
        await server.stop()


app = FastAPI(title="Memory Builder", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(config.WEB_DIR)), name="static")


@app.middleware("http")
async def no_cache_ui(request: Request, call_next):
    """Never let a stale tab keep old HTML/JS after a prompt or UI change."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


# --- request models ---------------------------------------------------------
class PromptPayload(BaseModel):
    system_prompt: Optional[str] = None
    run_prompt: Optional[str] = None
    base_revision: Optional[str] = None
    prompt_comment: Optional[str] = Field(default=None, max_length=200)


class RunPayload(BaseModel):
    cycles: int = Field(default=1, ge=1, le=1000)
    model: dict
    system_prompt: Optional[str] = None
    run_prompt: Optional[str] = None
    out_name: Optional[str] = None
    base_revision: Optional[str] = None


class StopPayload(BaseModel):
    pass


class ExamplePayload(BaseModel):
    record: dict


# --- routes -----------------------------------------------------------------
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(config.WEB_DIR / "index.html")


@app.get("/api/health")
async def health(request: Request) -> dict:
    client: OpenCodeClient = request.app.state.client
    try:
        info = await client.info()
        return {"ok": True, "opencode": info, "base_url": client.server.base_url}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)


@app.get("/api/models")
async def models(request: Request) -> dict:
    client: OpenCodeClient = request.app.state.client
    catalog = await client.list_models(str(config.BASE_DIR))
    result = []
    for m in catalog:
        if not config.is_free_or_ollama(m):
            continue
        result.append(
            {
                "providerID": m.get("providerID"),
                "id": m.get("id"),
                "name": m.get("name") or m.get("id"),
                "variants": [v.get("id") for v in m.get("variants", [])],
                "reasoning": bool(m.get("variants")),
            }
        )
    result.sort(key=lambda x: (x["providerID"], x["id"]))
    return {"models": result}


@app.get("/api/config")
async def get_config(request: Request) -> dict:
    client: OpenCodeClient = request.app.state.client
    agents = await client.list_agents(str(config.BASE_DIR))
    settings = load_settings()
    current, store = ensure_prompt_version()
    return {
        "agent": config.AGENT_NAME,
        "agent_present": any(a.get("id") == config.AGENT_NAME for a in agents),
        "system_prompt": read_agent_system(),
        "run_prompt": load_run_prompt(),
        "last_model": settings.get("last_model"),
        "prompt_revision": prompt_revision(),
        "prompt_version": current["version"],
        "prompt_version_comment": current.get("comment", ""),
        "prompt_versions": prompt_version_summaries(store),
        "directory": str(config.BASE_DIR),
    }


@app.post("/api/config/prompts")
async def save_prompts(payload: PromptPayload, request: Request) -> dict:
    client: OpenCodeClient = request.app.state.client
    if (
        payload.system_prompt is not None or payload.run_prompt is not None
    ) and not payload.base_revision:
        raise HTTPException(
            status_code=409,
            detail="Reload the page before saving prompts.",
        )
    if payload.base_revision and payload.base_revision != prompt_revision():
        raise HTTPException(
            status_code=409,
            detail=(
                "Prompts changed on the server since this page loaded. "
                "Click Reload prompts, then save again."
            ),
        )

    # Capture any prompt edit made outside the UI before this save.
    ensure_prompt_version()

    system_prompt = (
        payload.system_prompt
        if payload.system_prompt is not None
        else read_agent_system()
    ).strip()
    run_prompt = (
        payload.run_prompt
        if payload.run_prompt is not None
        else load_run_prompt()
    )
    changed = _prompt_hash(system_prompt, run_prompt) != prompt_revision()
    comment = (payload.prompt_comment or "").strip()
    if changed and not comment:
        raise HTTPException(
            status_code=400,
            detail="Add a short comment describing what changed in the prompts.",
        )

    if payload.system_prompt is not None:
        write_agent(system_prompt)
    if payload.run_prompt is not None:
        save_run_prompt(run_prompt)

    if changed:
        store = load_prompt_store()
        record = _append_prompt_version(
            store,
            read_agent_system(),
            load_run_prompt(),
            comment,
        )
        _write_prompt_store(store)
    else:
        store = load_prompt_store()
        record = _current_prompt_version(store)

    await client.reload_location(str(config.BASE_DIR))
    return {
        "ok": True,
        "changed": changed,
        "prompt_revision": prompt_revision(),
        "prompt_version": record["version"],
        "prompt_version_comment": record.get("comment", ""),
        "prompt_versions": prompt_version_summaries(store),
        "system_prompt": read_agent_system(),
        "run_prompt": load_run_prompt(),
    }


@app.get("/api/config/prompts/versions/{version}")
async def get_prompt_version(version: int) -> dict:
    store = load_prompt_store()
    record = next(
        (v for v in store.get("versions", []) if v.get("version") == version),
        None,
    )
    if not record:
        raise HTTPException(status_code=404, detail="prompt version not found")
    return {
        "version": record["version"],
        "created": record.get("created", ""),
        "comment": record.get("comment", ""),
        "revision": record.get("revision", ""),
        "system_prompt": record.get("system_prompt", ""),
        "run_prompt": record.get("run_prompt", ""),
        "current": record["version"] == store.get("current"),
    }


@app.get("/api/barygraph")
async def barygraph(request: Request) -> dict:
    client: OpenCodeClient = request.app.state.client
    mcp = await client.list_mcp(str(config.BASE_DIR))
    return {
        "servers": mcp,
        "url": config.BARYGRAPH_MCP_URL,
    }


@app.post("/api/run")
async def run(payload: RunPayload, request: Request) -> dict:
    client: OpenCodeClient = request.app.state.client
    if payload.system_prompt is not None and not payload.base_revision:
        raise HTTPException(
            status_code=409,
            detail="Reload the page before starting a run.",
        )
    if payload.base_revision and payload.base_revision != prompt_revision():
        raise HTTPException(
            status_code=409,
            detail=(
                "Prompts changed on the server since this page loaded. "
                "Reload the page before starting a run."
            ),
        )
    saved_system = read_agent_system()
    saved_run = load_run_prompt()
    if payload.system_prompt is not None and payload.system_prompt.strip() != saved_system:
        raise HTTPException(
            status_code=409,
            detail=(
                "Prompt edits must be saved with a version comment before "
                "starting a run."
            ),
        )
    if payload.run_prompt is not None and payload.run_prompt != saved_run:
        raise HTTPException(
            status_code=409,
            detail=(
                "Prompt edits must be saved with a version comment before "
                "starting a run."
            ),
        )
    current, _store = ensure_prompt_version()
    out_path = new_output_path()
    if payload.out_name:
        safe = Path(payload.out_name).name
        out_path = str(config.OUTPUT_DIR / (safe if safe.endswith(".jsonl") else safe + ".jsonl"))
    save_settings(last_model=payload.model)
    job = request.app.state.jobs.start(
        directory=str(config.BASE_DIR),
        agent=config.AGENT_NAME,
        model=payload.model,
        cycles=payload.cycles,
        run_prompt=saved_run,
        system_prompt=saved_system,
        out_path=out_path,
        prompt_version=current["version"],
        prompt_revision=current["revision"],
        prompt_comment=current.get("comment", ""),
    )
    return {
        "job": job.summary(),
        "prompt_revision": prompt_revision(),
        "prompt_version": current["version"],
    }


async def _sse(job):
    while True:
        item = await job.queue.get()
        if item is None:
            break
        yield f"event: {item['event']}\ndata: {json.dumps(item['data'], ensure_ascii=False)}\n\n"


@app.get("/api/run/{job_id}/events")
async def run_events(job_id: str, request: Request) -> StreamingResponse:
    job = request.app.state.jobs.jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return StreamingResponse(
        _sse(job),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/run/{job_id}/stop")
async def run_stop(job_id: str, request: Request) -> dict:
    await request.app.state.jobs.stop(job_id)
    return {"ok": True}


@app.get("/api/run/{job_id}")
async def run_status(job_id: str, request: Request) -> dict:
    job = request.app.state.jobs.jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return {**job.summary(), "results": job.results}


@app.get("/api/runs")
async def list_runs() -> dict:
    runs = []
    for path in sorted(config.RUNS_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        stat = path.stat()
        runs.append({"name": path.name, "size": stat.st_size, "mtime": stat.st_mtime})
    return {"runs": runs, "directory": str(config.RUNS_DIR)}


@app.get("/api/runs/{name}")
async def get_run(name: str) -> dict:
    safe = Path(name).name
    if safe == config.EXAMPLES_FILE.name:
        raise HTTPException(status_code=400, detail="not a run file")
    path = config.RUNS_DIR / safe
    if not path.exists():
        path = config.OUTPUT_DIR / safe  # legacy fallback
    if not path.exists():
        raise HTTPException(status_code=404, detail="file not found")
    text = path.read_text(encoding="utf-8")
    records = [parse_json_object(line) for line in text.splitlines() if line.strip()]
    return {"name": safe, "records": records, "raw": text}


@app.get("/api/runs/{name}/download")
async def download_run(name: str) -> FileResponse:
    safe = Path(name).name
    if safe == config.EXAMPLES_FILE.name:
        raise HTTPException(status_code=400, detail="not a run file")
    path = config.RUNS_DIR / safe
    if not path.exists():
        path = config.OUTPUT_DIR / safe  # legacy fallback
    if not path.exists():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(
        path,
        media_type="application/x-ndjson",
        filename=safe,
    )


@app.delete("/api/runs/{name}")
async def delete_run(name: str) -> dict:
    safe = Path(name).name
    if safe == config.EXAMPLES_FILE.name:
        raise HTTPException(status_code=400, detail="not a run file")
    path = config.RUNS_DIR / safe
    if not path.exists():
        path = config.OUTPUT_DIR / safe  # legacy fallback
    if not path.exists():
        raise HTTPException(status_code=404, detail="file not found")
    path.unlink()
    return {"ok": True}


@app.get("/api/examples/download")
async def download_examples() -> FileResponse:
    if not config.EXAMPLES_FILE.exists():
        raise HTTPException(status_code=404, detail="examples file not found")
    return FileResponse(
        config.EXAMPLES_FILE,
        media_type="application/x-ndjson",
        filename="examples.jsonl",
    )


@app.get("/api/examples")
async def list_examples() -> dict:
    records = read_examples()
    return {
        "name": config.EXAMPLES_FILE.name,
        "path": str(config.EXAMPLES_FILE),
        "records": records,
        "count": len(records),
    }


@app.post("/api/examples")
async def add_example(payload: ExamplePayload) -> dict:
    records = read_examples()
    key = json.dumps(payload.record, sort_keys=True, ensure_ascii=False)
    existing = {json.dumps(r, sort_keys=True, ensure_ascii=False) for r in records}
    if key in existing:
        return {"ok": True, "duplicate": True, "count": len(records)}
    config.ensure_dirs()
    with open(config.EXAMPLES_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload.record, ensure_ascii=False) + "\n")
    return {"ok": True, "duplicate": False, "count": len(records) + 1}


@app.delete("/api/examples/{index}")
async def delete_example(index: int) -> dict:
    records = read_examples()
    if index < 0 or index >= len(records):
        raise HTTPException(status_code=404, detail="example not found")
    records.pop(index)
    config.EXAMPLES_FILE.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
        encoding="utf-8",
    )
    return {"ok": True, "count": len(records)}
