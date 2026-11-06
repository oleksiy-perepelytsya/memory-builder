"""Cycle orchestration: drive the model N times and append JSONL records."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from . import config
from .opencode import OpenCodeClient

log = logging.getLogger("memory_builder.runner")

_FENCE_RE = re.compile(r"^\s*```(?:jsonl?|JSON)?\s*|\s*```\s*$")


def parse_json_object(text: str) -> Optional[Any]:
    """Best-effort extraction of a single JSON value from model output."""
    if not text:
        return None
    cleaned = text.strip()
    # Drop a single surrounding markdown code fence if present.
    if cleaned.startswith("```"):
        first_nl = cleaned.find("\n")
        if first_nl != -1:
            cleaned = cleaned[first_nl + 1 :]
        cleaned = re.sub(r"```\s*$", "", cleaned).strip()

    decoder = json.JSONDecoder()
    # Try whole string first.
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Scan for the first decodable value starting at a brace/bracket.
    for i, ch in enumerate(cleaned):
        if ch in "[{":
            try:
                value, _ = decoder.raw_decode(cleaned[i:])
                return value
            except json.JSONDecodeError:
                continue
    return None


# Native (barygraph_sample_metabary), Code Mode dot
# (tools.barygraph.sample_metabary), and bracket
# (tools.barygraph["sample_metabary"] /
# tools["barygraph"].sample_metabary) references.
_BARYGRAPH_REF_RE = re.compile(
    r"barygraph(?:__?|\.|\[\s*[\"']|[\"']\s*\]\s*\.)([a-z_]+)"
)


def _tool_label(name: str, tool_input: Any) -> str:
    """Turn a Code Mode `execute` call into a readable BaryGraph tool label."""
    if name != "execute" or not isinstance(tool_input, dict):
        return name
    code = tool_input.get("code") or ""
    if "barygraph" not in code:
        return name
    found = []
    for match in _BARYGRAPH_REF_RE.findall(code):
        if match not in found:
            found.append(match)
    return "barygraph: " + ", ".join(found) if found else "execute (barygraph)"


def _extract_error(messages: list[dict]) -> Optional[dict]:
    """Return the provider error for a failed turn, if any."""
    for msg in reversed(messages):
        if msg.get("type") == "assistant" and msg.get("finish") == "error":
            err = msg.get("error") or {}
            return {
                "type": err.get("type") or "provider_error",
                "message": err.get("message"),
                "status": err.get("status"),
            }
        if msg.get("type") == "idle" and msg.get("outcome") == "failed":
            err = msg.get("error") or {}
            return {
                "type": err.get("type") or "provider_error",
                "message": err.get("message"),
                "status": err.get("status"),
            }
    return None


def _is_rate_limited(error: dict) -> bool:
    if not error:
        return False
    if error.get("status") == 429:
        return True
    kind = (error.get("type") or "").lower()
    message = (error.get("message") or "").lower()
    return any(token in kind or token in message for token in ("quota", "rate limit", "rate-limit"))


def _retry_delay(error: dict) -> float:
    message = error.get("message") or ""
    match = re.search(r"retry in ([\d.]+)\s*s", message, re.IGNORECASE)
    if match:
        return min(float(match.group(1)) + 1.0, 120.0)
    return 30.0


def _assistant_extract(messages: list[dict]) -> tuple[str, str, list[dict]]:
    """Return (final_text, reasoning, tools) from a turn's assistant messages.

    A turn may contain several assistant messages (tool-call steps followed by a
    final answer), so we take the text/reasoning from the last message that has
    any, and aggregate tool calls across the whole turn.
    """
    final_text, reasoning, tools = "", "", []
    for msg in messages:
        if msg.get("type") != "assistant":
            continue
        text_parts, reason_parts = [], []
        for part in msg.get("content", []):
            ptype = part.get("type")
            if ptype == "text":
                text_parts.append(part.get("text", ""))
            elif ptype == "reasoning":
                reason_parts.append(part.get("text", ""))
            elif ptype == "tool":
                state = part.get("state", {})
                tool_input = state.get("input") if isinstance(state, dict) else None
                tools.append(
                    {
                        "id": part.get("id"),
                        "name": _tool_label(part.get("name"), tool_input),
                        "raw_name": part.get("name"),
                        "status": state.get("status") if isinstance(state, dict) else None,
                        "input": tool_input,
                    }
                )
        text = "\n".join(text_parts).strip()
        reason = "\n".join(reason_parts).strip()
        if text:
            final_text = text
        if reason:
            reasoning = reason
    return final_text, reasoning, tools


@dataclass
class Job:
    id: str
    directory: str
    agent: str
    model: dict
    cycles: int
    run_prompt: str
    system_prompt: str
    out_path: str
    prompt_version: int = 0
    prompt_revision: str = ""
    prompt_comment: str = ""
    status: str = "pending"
    error: Optional[str] = None
    created: float = field(default_factory=time.time)
    results: list[dict] = field(default_factory=list)
    session_ids: set[str] = field(default_factory=set)
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    current_session: Optional[str] = None
    current_done: Optional[asyncio.Event] = None
    saw_event: bool = False
    last_event_at: float = 0.0
    task: Optional[asyncio.Task] = None
    stop_requested: bool = False
    tool_names: dict[str, str] = field(default_factory=dict)

    def summary(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "cycles": self.cycles,
            "completed": len(self.results),
            "model": self.model,
            "agent": self.agent,
            "out_path": self.out_path,
            "prompt_version": self.prompt_version,
            "prompt_version_comment": self.prompt_comment,
            "created": self.created,
            "error": self.error,
        }


class JobManager:
    def __init__(self, client: OpenCodeClient):
        self.client = client
        self.jobs: dict[str, Job] = {}

    def start(
        self,
        *,
        directory: str,
        agent: str,
        model: dict,
        cycles: int,
        run_prompt: str,
        system_prompt: str,
        out_path: str,
        prompt_version: int,
        prompt_revision: str,
        prompt_comment: str,
    ) -> Job:
        job = Job(
            id=uuid.uuid4().hex[:12],
            directory=directory,
            agent=agent,
            model=model,
            cycles=cycles,
            run_prompt=run_prompt,
            system_prompt=system_prompt,
            out_path=out_path,
            prompt_version=prompt_version,
            prompt_revision=prompt_revision,
            prompt_comment=prompt_comment,
        )
        self.jobs[job.id] = job
        job.task = asyncio.create_task(self._run(job))
        return job

    async def stop(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if not job:
            return
        job.stop_requested = True
        if job.current_session:
            await self.client.abort(job.current_session)
        if job.current_done:
            job.current_done.set()

    # -- internals ----------------------------------------------------------
    async def _emit(self, job: Job, event: str, data: dict) -> None:
        await job.queue.put({"event": event, "data": data})

    async def _run(self, job: Job) -> None:
        job.status = "running"
        await self.client.reload_location(job.directory)
        await self._emit(job, "job.started", {"job": job.summary()})
        listener = asyncio.create_task(self._listen(job))
        try:
            with open(job.out_path, "a", encoding="utf-8") as out_file:
                for cycle in range(1, job.cycles + 1):
                    if job.stop_requested:
                        break
                    await self._run_cycle(job, cycle, out_file)
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
            log.exception("job %s failed", job.id)
            job.status = "failed"
            job.error = str(exc)
        finally:
            listener.cancel()
            if job.status == "running":
                job.status = "stopped" if job.stop_requested else "completed"
            self._write_state(job)
            await self._emit(
                job,
                "job.completed",
                {
                    "status": job.status,
                    "error": job.error,
                    "out_path": job.out_path,
                    "prompt_version": job.prompt_version,
                },
            )
            await job.queue.put(None)  # sentinel: close SSE stream

    async def _run_cycle(self, job: Job, cycle: int, out_file) -> None:
        await self._emit(job, "cycle.started", {"cycle": cycle})
        max_attempts = config.CYCLE_RETRIES + 1
        attempt = 0
        while True:
            attempt += 1
            session_id, timed_out, no_response, final_text, reasoning, tools, error = await self._attempt(job, cycle)
            if (
                not timed_out
                and _is_rate_limited(error)
                and attempt < max_attempts
                and not job.stop_requested
            ):
                delay = _retry_delay(error)
                await self._emit(
                    job,
                    "cycle.retry",
                    {"cycle": cycle, "attempt": attempt, "delay": delay, "error": error},
                )
                await asyncio.sleep(delay)
                if job.stop_requested:
                    break
                continue
            break

        obj = parse_json_object(final_text)

        if no_response:
            status = "failed"
            record = {
                "_error": "no_response",
                "_message": (
                    f"The provider sent nothing within {config.FIRST_EVENT_TIMEOUT}s; "
                    "the model may be unavailable or overloaded."
                ),
                "_cycle": cycle,
                "_raw": final_text,
            }
        elif timed_out:
            status = "failed"
            record = {
                "_error": "timeout",
                "_message": f"No completion after {config.CYCLE_TIMEOUT}s.",
                "_cycle": cycle,
                "_raw": final_text,
            }
        elif error and not final_text:
            status = "failed"
            record = {
                "_error": error.get("type") or "provider_error",
                "_message": error.get("message"),
                "_status": error.get("status"),
                "_cycle": cycle,
            }
        elif obj is None:
            record = {"_error": "unparsed_output", "_raw": final_text, "_cycle": cycle}
            status = "failed"
        else:
            record = obj
            status = "completed"

        # Stamp every record with the exact prompt version used for the run.
        if isinstance(record, dict):
            record["_prompt_version"] = job.prompt_version
            record["_prompt_revision"] = job.prompt_revision
            record["_prompt_comment"] = job.prompt_comment

        out_file.write(json.dumps(record, ensure_ascii=False) + "\n")
        out_file.flush()

        result = {
            "cycle": cycle,
            "status": status,
            "object": obj,
            "raw": final_text,
            "reasoning": reasoning,
            "tools": tools,
            "error": error,
            "record": record,
            "session_id": session_id,
            "attempts": attempt,
        }
        job.results.append(result)
        await self._emit(job, "cycle.completed", result)

    async def _attempt(self, job: Job, cycle: int):
        """Run one model turn.

        Returns (session_id, timed_out, no_response, text, reasoning, tools, error).
        """
        session = await self.client.create_session(
            job.directory, job.agent, job.model, title=f"memory-builder c{cycle}"
        )
        session_id = session.get("id")
        job.current_session = session_id
        job.session_ids.add(session_id)
        job.current_done = asyncio.Event()
        job.saw_event = False
        job.last_event_at = time.time()

        await self.client.prompt(session_id, job.run_prompt)

        started = time.time()
        timed_out = False
        no_response = False
        next_notice = started + config.FIRST_EVENT_NOTICE
        while not job.current_done.is_set():
            now = time.time()
            if not job.saw_event and now >= next_notice:
                await self._emit(
                    job,
                    "cycle.waiting",
                    {
                        "cycle": cycle,
                        "seconds": int(now - started),
                        "message": "Waiting for the provider's first response…",
                    },
                )
                next_notice = now + max(5, config.FIRST_EVENT_NOTICE)
            # Check the no-response limit first so it still reports `no_response`
            # when it is equal to the overall cycle timeout.
            if not job.saw_event and now - started >= config.FIRST_EVENT_TIMEOUT:
                timed_out = True
                no_response = True
                break
            hard_left = started + config.CYCLE_TIMEOUT - now
            if hard_left <= 0:
                timed_out = True
                break
            if not job.saw_event:
                first_left = job.last_event_at + config.FIRST_EVENT_TIMEOUT - now
                timeout = min(hard_left, max(first_left, 0.1), 10.0)
            else:
                timeout = min(hard_left, 10.0)
            try:
                await asyncio.wait_for(job.current_done.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass

        if timed_out:
            await self.client.abort(session_id)

        # Give the server a moment to flush the final message.
        await asyncio.sleep(0.3)
        messages = await self.client.messages(session_id)
        final_text, reasoning, tools = _assistant_extract(messages)
        error = _extract_error(messages)
        return session_id, timed_out, no_response, final_text, reasoning, tools, error

    async def _listen(self, job: Job) -> None:
        """Route live events for this job's sessions to the SSE queue."""
        try:
            async for ev in self.client.events():
                etype = ev.get("type", "")
                payload = ev.get("data")
                if not isinstance(payload, dict):
                    payload = {}
                session_id = payload.get("sessionID")
                log.debug("job %s event %s session=%s", job.id, etype, session_id)
                if session_id not in job.session_ids:
                    continue
                cycle = self._cycle_for(job, session_id)
                # Only model output counts as "the provider is alive" — session
                # lifecycle events alone must not defeat the no-response timeout.
                if session_id == job.current_session and etype in (
                    "session.reasoning.delta",
                    "session.text.delta",
                    "session.tool.input.started",
                    "session.tool.success",
                    "session.tool.failed",
                    "session.idle",
                    "session.execution.succeeded",
                    "session.execution.failed",
                ):
                    job.saw_event = True
                    job.last_event_at = time.time()
                if etype == "session.reasoning.delta":
                    await self._emit(job, "reasoning.delta", {"cycle": cycle, "text": payload.get("delta", "")})
                elif etype == "session.text.delta":
                    await self._emit(job, "text.delta", {"cycle": cycle, "text": payload.get("delta", "")})
                elif etype == "session.tool.input.started":
                    name = payload.get("name")
                    job.tool_names[payload.get("id")] = name
                    await self._emit(job, "tool.call", {"cycle": cycle, "name": name})
                elif etype in ("session.tool.success", "session.tool.failed"):
                    name = payload.get("name") or job.tool_names.get(payload.get("id"))
                    await self._emit(
                        job, "tool.result",
                        {"cycle": cycle, "name": name, "ok": etype.endswith("success")},
                    )
                elif etype in ("session.idle", "session.execution.succeeded", "session.execution.failed"):
                    if session_id == job.current_session and job.current_done and not job.current_done.is_set():
                        job.current_done.set()
                else:
                    await self._emit(job, "session.event", {"cycle": cycle, "type": etype})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("event listener stopped: %s", exc)

    @staticmethod
    def _cycle_for(job: Job, session_id: Optional[str]) -> Optional[int]:
        for result in reversed(job.results):
            if result.get("session_id") == session_id:
                return result["cycle"]
        return len(job.results) + 1

    def _write_state(self, job: Job) -> None:
        try:
            path = config.JOBS_DIR / f"{job.id}.json"
            path.write_text(
                json.dumps(
                    {
                        "id": job.id,
                        "status": job.status,
                        "error": job.error,
                        "model": job.model,
                        "agent": job.agent,
                        "cycles": job.cycles,
                        "out_path": job.out_path,
                        "prompt_version": job.prompt_version,
                        "prompt_revision": job.prompt_revision,
                        "prompt_comment": job.prompt_comment,
                        "results": job.results,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("could not write job state: %s", exc)


def new_output_path() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = config.RUNS_DIR / f"{stamp}.jsonl"
    n = 1
    while path.exists():
        n += 1
        path = config.RUNS_DIR / f"{stamp}-{n}.jsonl"
    return str(path)
