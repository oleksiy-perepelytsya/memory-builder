"""Identity pilot runner: think -> extract triads -> retrieve memory -> repeat."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from . import config, pilot_memory
from .opencode import OpenCodeClient
from .runner import (
    _assistant_extract,
    _extract_error,
    _is_rate_limited,
    _retry_delay,
    parse_json_object,
)

log = logging.getLogger("memory_builder.pilot")


class PilotTurnError(RuntimeError):
    def __init__(self, kind: str, message: str, raw: str = ""):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.raw = raw


@dataclass
class PilotJob:
    id: str
    directory: str
    agent: str
    model: dict
    question: str
    total_steps: int
    top_k: int
    prompts: dict
    out_path: str
    prompt_version: int = 0
    prompt_revision: str = ""
    prompt_comment: str = ""
    status: str = "pending"
    error: Optional[str] = None
    created: float = field(default_factory=time.time)
    trajectory: list[dict] = field(default_factory=list)
    result: Optional[dict] = None
    pending_hits: list[dict] = field(default_factory=list)
    # canonical triad key -> last step where it was used or retrieved
    recent_hit_keys: dict[str, int] = field(default_factory=dict)
    session_meta: dict[str, dict] = field(default_factory=dict)
    tool_names: dict[str, str] = field(default_factory=dict)
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    current_session: Optional[str] = None
    current_done: Optional[asyncio.Event] = None
    saw_event: bool = False
    last_event_at: float = 0.0
    task: Optional[asyncio.Task] = None
    stop_requested: bool = False

    def summary(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "question": self.question,
            "model": self.model,
            "agent": self.agent,
            "total_steps": self.total_steps,
            "steps_done": len(self.trajectory),
            "top_k": self.top_k,
            "out_path": self.out_path,
            "prompt_version": self.prompt_version,
            "prompt_version_comment": self.prompt_comment,
            "created": self.created,
            "error": self.error,
        }


class PilotManager:
    def __init__(self, client: OpenCodeClient):
        self.client = client
        self.jobs: dict[str, PilotJob] = {}

    def start(
        self,
        *,
        directory: str,
        agent: str,
        model: dict,
        question: str,
        total_steps: int,
        top_k: int,
        prompts: dict,
        out_path: str,
        prompt_version: int,
        prompt_revision: str,
        prompt_comment: str,
    ) -> PilotJob:
        job = PilotJob(
            id=uuid.uuid4().hex[:12],
            directory=directory,
            agent=agent,
            model=model,
            question=question,
            total_steps=total_steps,
            top_k=top_k,
            prompts=prompts,
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

    async def stop_all(self) -> None:
        await asyncio.gather(
            *(self.stop(job_id) for job_id in list(self.jobs)),
            return_exceptions=True,
        )

    # -- internals ----------------------------------------------------------
    async def _emit(self, job: PilotJob, event: str, data: dict) -> None:
        await job.queue.put({"event": event, "data": data})

    async def _run(self, job: PilotJob) -> None:
        job.status = "running"
        await self.client.reload_location(job.directory)
        await self._emit(job, "pilot.started", {"job": job.summary()})
        listener = asyncio.create_task(self._listen(job))
        with open(job.out_path, "a", encoding="utf-8") as out_file:
            try:
                for step in range(1, job.total_steps + 1):
                    if job.stop_requested:
                        break
                    await self._run_step(job, step)
                if job.stop_requested:
                    job.status = "stopped"
                    record = self._partial_record(job, stopped=True)
                else:
                    job.status = "completed"
                    record = self._final_record(job)
                job.result = record
                out_file.write(json.dumps(record, ensure_ascii=False) + "\n")
                out_file.flush()
            except Exception as exc:  # noqa: BLE001 - surface to the pilot UI
                log.exception("pilot job %s failed", job.id)
                job.status = "failed"
                job.error = str(exc)
                record = self._error_record(job, exc)
                job.result = record
                out_file.write(json.dumps(record, ensure_ascii=False) + "\n")
                out_file.flush()
            finally:
                listener.cancel()
                self._write_state(job)
                await self._emit(
                    job,
                    "pilot.completed",
                    {
                        "status": job.status,
                        "error": job.error,
                        "out_path": job.out_path,
                        "prompt_version": job.prompt_version,
                        "result": job.result,
                        "steps": job.trajectory,
                    },
                )
                await job.queue.put(None)

    async def _run_step(self, job: PilotJob, step: int) -> None:
        previous = job.trajectory[-1] if job.trajectory else None
        injected_hits = job.pending_hits if step > 1 else []
        await self._emit(
            job,
            "step.started",
            {
                "step": step,
                "total": job.total_steps,
                "memory_hits": injected_hits,
            },
        )

        think_prompt = self._think_prompt(job, step, previous, injected_hits)
        think = await self._turn_with_retry(job, step, "think", think_prompt)
        thought = (think.get("text") or "").strip()
        if not thought:
            if think.get("timed_out"):
                raise PilotTurnError(
                    "timeout",
                    f"Step {step} think turn timed out.",
                    think.get("text") or "",
                )
            if think.get("no_response"):
                raise PilotTurnError(
                    "no_response",
                    f"Step {step} think turn produced no response.",
                    think.get("text") or "",
                )
            error = think.get("error") or {}
            raise PilotTurnError(
                error.get("type") or "empty_thought",
                error.get("message") or f"Step {step} produced no thought text.",
                think.get("text") or "",
            )

        step_record: dict[str, Any] = {
            "step": step,
            "thought": thought,
            "reasoning": think.get("reasoning") or "",
            "tools": think.get("tools") or [],
            "think_session_id": think.get("session_id"),
            "memory_hits": injected_hits,
            "constructs": [],
            "retrieved_hits": [],
            "memory_shift": "none",
            "shift_note": None,
            "extract_warning": None,
            "extract_raw": "",
        }
        await self._emit(
            job,
            "step.thought",
            {
                "step": step,
                "thought": thought,
                "reasoning": think.get("reasoning") or "",
                "tools": think.get("tools") or [],
            },
        )
        if job.stop_requested:
            job.trajectory.append(step_record)
            return

        extract_prompt = self._extract_prompt(job, step, thought, injected_hits)
        extract = await self._turn_with_retry(job, step, "extract", extract_prompt)
        parsed = self._parse_extract(extract.get("text") or "")
        if parsed is None:
            retry_prompt = (
                extract_prompt
                + "\n\nYour previous answer was not valid JSON. Return ONLY a JSON "
                "object with a `constructs` array and the `memory_shift` / "
                "`shift_note` fields."
            )
            extract = await self._turn_with_retry(
                job, step, "extract-retry", retry_prompt
            )
            parsed = self._parse_extract(extract.get("text") or "")

        if parsed is None:
            step_record["extract_warning"] = (
                "Could not parse a constructs JSON object; continuing without memory."
            )
            step_record["extract_raw"] = extract.get("text") or ""
            constructs: list[dict] = []
            shift = "unparsed"
            shift_note = None
        else:
            constructs = parsed["constructs"]
            shift = parsed.get("memory_shift") or "none"
            shift_note = parsed.get("shift_note")
            step_record["extract_raw"] = extract.get("text") or ""

        exclude_keys = {
            key
            for key, last_step in job.recent_hit_keys.items()
            if step - last_step <= config.PILOT_MEMORY_EXCLUDE_WINDOW
        }
        try:
            hits = pilot_memory.search_constructs(
                constructs,
                top_k=job.top_k,
                exclude_run_id=job.id,
                exclude_step=step,
                exclude_keys=exclude_keys,
                min_score=config.PILOT_MEMORY_MIN_SCORE,
                min_shared=config.PILOT_MEMORY_MIN_SHARED,
                one_per_run=True,
            )
        except Exception as exc:  # noqa: BLE001 - memory is best effort
            log.warning("pilot memory search failed: %s", exc)
            hits = []
            step_record["extract_warning"] = (
                step_record["extract_warning"] or f"Memory search failed: {exc}"
            )
        records = [
            pilot_memory.make_record(
                construct,
                run_id=job.id,
                step=step,
                question=job.question,
                model=_model_label(job.model),
                source_excerpt=construct.get("source_excerpt") or "",
            )
            for construct in constructs
        ]
        try:
            pilot_memory.append_records(records)
        except OSError as exc:
            log.warning("pilot memory append failed: %s", exc)
            step_record["extract_warning"] = (
                step_record["extract_warning"] or f"Memory append failed: {exc}"
            )
        for hit in hits:
            job.recent_hit_keys[pilot_memory.novelty_key(hit)] = step
        for record in records:
            job.recent_hit_keys[pilot_memory.novelty_key(record)] = step

        step_record.update(
            {
                "constructs": constructs,
                "retrieved_hits": hits,
                "memory_shift": shift,
                "shift_note": shift_note,
                "extract_reasoning": extract.get("reasoning") or "",
            }
        )
        job.pending_hits = hits
        job.trajectory.append(step_record)

        await self._emit(
            job,
            "step.constructs",
            {
                "step": step,
                "constructs": constructs,
                "memory_shift": shift,
                "shift_note": shift_note,
                "warning": step_record["extract_warning"],
            },
        )
        await self._emit(
            job,
            "step.memory",
            {"step": step, "hits": hits},
        )
        await self._emit(job, "step.completed", step_record)

    async def _turn_with_retry(
        self, job: PilotJob, step: int, stage: str, prompt: str
    ) -> dict:
        attempts = max(1, config.PILOT_TURN_RETRIES + 1)
        result: dict = {}
        for attempt in range(1, attempts + 1):
            result = await self._run_turn(job, step, stage, prompt)
            error = result.get("error") or {}
            if (
                _is_rate_limited(error)
                and attempt < attempts
                and not job.stop_requested
            ):
                delay = _retry_delay(error)
                await self._emit(
                    job,
                    "pilot.retry",
                    {
                        "step": step,
                        "stage": stage,
                        "attempt": attempt,
                        "delay": delay,
                        "error": error,
                    },
                )
                await asyncio.sleep(delay)
                if job.stop_requested:
                    break
                continue
            return result
        return result

    async def _run_turn(
        self, job: PilotJob, step: int, stage: str, prompt: str
    ) -> dict:
        session = await self.client.create_session(
            job.directory,
            job.agent,
            job.model,
            title=f"pilot-{job.id[:6]} s{step} {stage}",
        )
        session_id = session.get("id")
        job.current_session = session_id
        job.session_meta[session_id] = {"step": step, "stage": stage}
        job.current_done = asyncio.Event()
        job.saw_event = False
        job.last_event_at = time.time()

        await self.client.prompt(session_id, prompt)

        started = time.time()
        timed_out = False
        no_response = False
        next_notice = started + config.PILOT_FIRST_EVENT_NOTICE
        while not job.current_done.is_set():
            now = time.time()
            if not job.saw_event and now >= next_notice:
                await self._emit(
                    job,
                    "pilot.waiting",
                    {
                        "step": step,
                        "stage": stage,
                        "seconds": int(now - started),
                        "message": "Waiting for the model's first response…",
                    },
                )
                next_notice = now + max(5, config.PILOT_FIRST_EVENT_NOTICE)
            if (
                not job.saw_event
                and now - started >= config.PILOT_FIRST_EVENT_TIMEOUT
            ):
                timed_out = True
                no_response = True
                break
            hard_left = started + config.PILOT_STEP_TIMEOUT - now
            if hard_left <= 0:
                timed_out = True
                break
            if not job.saw_event:
                first_left = (
                    job.last_event_at + config.PILOT_FIRST_EVENT_TIMEOUT - now
                )
                timeout = min(hard_left, max(first_left, 0.1), 10.0)
            else:
                timeout = min(hard_left, 10.0)
            try:
                await asyncio.wait_for(job.current_done.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass

        if timed_out:
            await self.client.abort(session_id)

        # Let the server flush the final assistant message.
        await asyncio.sleep(0.3)
        messages = await self.client.messages(session_id)
        final_text, reasoning, tools = _assistant_extract(messages)
        error = _extract_error(messages)
        return {
            "session_id": session_id,
            "text": final_text,
            "reasoning": reasoning,
            "tools": tools,
            "error": error,
            "timed_out": timed_out,
            "no_response": no_response,
        }

    async def _listen(self, job: PilotJob) -> None:
        try:
            async for ev in self.client.events():
                etype = ev.get("type", "")
                payload = ev.get("data")
                if not isinstance(payload, dict):
                    payload = {}
                session_id = payload.get("sessionID")
                meta = job.session_meta.get(session_id)
                if not meta:
                    continue
                step = meta.get("step")
                stage = meta.get("stage")
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
                base = {"step": step, "stage": stage}
                if etype == "session.reasoning.delta":
                    await self._emit(
                        job,
                        "reasoning.delta",
                        {**base, "text": payload.get("delta", "")},
                    )
                elif etype == "session.text.delta":
                    await self._emit(
                        job,
                        "text.delta",
                        {**base, "text": payload.get("delta", "")},
                    )
                elif etype == "session.tool.input.started":
                    name = payload.get("name")
                    job.tool_names[payload.get("id")] = name
                    await self._emit(job, "tool.call", {**base, "name": name})
                elif etype in ("session.tool.success", "session.tool.failed"):
                    name = payload.get("name") or job.tool_names.get(
                        payload.get("id")
                    )
                    await self._emit(
                        job,
                        "tool.result",
                        {**base, "name": name, "ok": etype.endswith("success")},
                    )
                elif etype in (
                    "session.idle",
                    "session.execution.succeeded",
                    "session.execution.failed",
                ):
                    if (
                        session_id == job.current_session
                        and job.current_done
                        and not job.current_done.is_set()
                    ):
                        job.current_done.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("pilot event listener stopped: %s", exc)

    @staticmethod
    def _parse_extract(text: str) -> Optional[dict]:
        value = parse_json_object(text)
        if isinstance(value, list):
            raw_constructs = value
            shift = "none"
            shift_note = None
        elif isinstance(value, dict):
            raw_constructs = value.get("constructs")
            if raw_constructs is None and any(
                key in value for key in ("cm1_terms", "bridge_terms", "cm2_terms")
            ):
                raw_constructs = [value]
            shift = value.get("memory_shift") or "none"
            shift_note = value.get("shift_note")
        else:
            return None
        if not isinstance(raw_constructs, list):
            return None

        constructs: list[dict] = []
        for raw in raw_constructs[:2]:
            if not isinstance(raw, dict):
                continue
            cm1 = pilot_memory.term_list(
                raw.get("cm1_terms") if "cm1_terms" in raw else raw.get("cm1")
            )
            bridge = pilot_memory.term_list(
                raw.get("bridge_terms")
                if "bridge_terms" in raw
                else raw.get("bridge")
            )
            cm2 = pilot_memory.term_list(
                raw.get("cm2_terms") if "cm2_terms" in raw else raw.get("cm2")
            )
            if not (cm1 or bridge or cm2):
                continue
            tension = raw.get("tension")
            if tension is not None and not isinstance(tension, str):
                tension = json.dumps(tension, ensure_ascii=False, sort_keys=True)
            constructs.append(
                {
                    "cm1_terms": cm1,
                    "bridge_terms": bridge,
                    "cm2_terms": cm2,
                    "tension": tension,
                    "rationale": str(raw.get("rationale") or ""),
                    "source_excerpt": str(raw.get("source_excerpt") or ""),
                }
            )
        return {
            "constructs": constructs,
            "memory_shift": shift,
            "shift_note": shift_note,
        }

    def _think_prompt(
        self,
        job: PilotJob,
        step: int,
        previous: Optional[dict],
        hits: list[dict],
    ) -> str:
        if step == 1 or previous is None:
            template = job.prompts["seed_prompt"]
            return _fill(template, {"QUESTION": job.question})
        template = job.prompts["step_prompt"]
        return _fill(
            template,
            {
                "QUESTION": job.question,
                "STEP": step,
                "TOTAL": job.total_steps,
                "TRAJECTORY": _render_trajectory(job.trajectory),
                "PREVIOUS_THOUGHT": previous.get("thought") or "",
                "PREVIOUS_CONSTRUCTS": _render_constructs(
                    previous.get("constructs") or []
                ),
                "MEMORY_HITS": _render_hits(hits),
            },
        )

    def _extract_prompt(
        self, job: PilotJob, step: int, thought: str, hits: list[dict]
    ) -> str:
        return _fill(
            job.prompts["extract_prompt"],
            {
                "QUESTION": job.question,
                "STEP": step,
                "TOTAL": job.total_steps,
                "THOUGHT": thought,
                "MEMORY_HITS": _render_hits(hits),
            },
        )

    def _final_record(self, job: PilotJob) -> dict:
        last = job.trajectory[-1] if job.trajectory else {}
        record: dict[str, Any] = {
            "id": f"identity-{job.id}",
            "title": "Identity trajectory",
            "question": job.question,
            "steps": job.trajectory,
            "final_constructs": last.get("constructs") or [],
            "trajectory_summary": _render_trajectory(job.trajectory, full=True),
            "author": _model_label(job.model),
            "_pilot": True,
            "_steps": job.total_steps,
            "_model": job.model,
        }
        _stamp(record, job)
        return record

    def _partial_record(self, job: PilotJob, stopped: bool) -> dict:
        last = job.trajectory[-1] if job.trajectory else {}
        record: dict[str, Any] = {
            "id": f"identity-{job.id}",
            "title": "Identity trajectory (partial)",
            "question": job.question,
            "steps": job.trajectory,
            "final_constructs": last.get("constructs") or [],
            "trajectory_summary": _render_trajectory(job.trajectory, full=True),
            "author": _model_label(job.model),
            "_pilot": True,
            "_steps": job.total_steps,
            "_completed_steps": len(job.trajectory),
            "_stopped": stopped,
            "_model": job.model,
        }
        _stamp(record, job)
        return record

    def _error_record(self, job: PilotJob, exc: Exception) -> dict:
        record: dict[str, Any] = {
            "_error": getattr(exc, "kind", "pilot_failed"),
            "_message": getattr(exc, "message", str(exc)),
            "_step": len(job.trajectory) + 1,
            "_question": job.question,
            "steps": job.trajectory,
            "_completed_steps": len(job.trajectory),
            "_model": job.model,
        }
        _stamp(record, job)
        return record

    def _write_state(self, job: PilotJob) -> None:
        try:
            path = config.PILOT_JOBS_DIR / f"{job.id}.json"
            path.write_text(
                json.dumps(
                    {
                        "job": job.summary(),
                        "steps": job.trajectory,
                        "result": job.result,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("could not write pilot job state: %s", exc)


def _fill(template: str, values: dict[str, Any]) -> str:
    out = template
    for key, value in values.items():
        out = out.replace("{{" + key + "}}", str(value if value is not None else ""))
    return out


def _render_trajectory(steps: list[dict], full: bool = False) -> str:
    if not steps:
        return "(no prior steps)"
    lines = []
    for step in steps:
        constructs = step.get("constructs") or []
        essence = ""
        if constructs:
            essence = constructs[0].get("rationale") or ""
        if not essence:
            essence = (step.get("thought") or "").strip().replace("\n", " ")
        if not full:
            essence = essence[:240]
        shift = step.get("memory_shift")
        suffix = f" [memory: {shift}]" if shift and shift != "none" else ""
        lines.append(f"Step {step.get('step')}: {essence}{suffix}")
    return "\n".join(lines)


def _render_constructs(constructs: list[dict]) -> str:
    if not constructs:
        return "(no constructs extracted)"
    lines = []
    for index, construct in enumerate(constructs, start=1):
        lines.append(f"Construct {index}:")
        lines.append(f"  cm1: {', '.join(construct.get('cm1_terms') or [])}")
        lines.append(f"  bridge: {', '.join(construct.get('bridge_terms') or [])}")
        lines.append(f"  cm2: {', '.join(construct.get('cm2_terms') or [])}")
        if construct.get("tension"):
            lines.append(f"  tension: {construct['tension']}")
        if construct.get("rationale"):
            lines.append(f"  rationale: {construct['rationale']}")
    return "\n".join(lines)


def _render_hits(hits: list[dict]) -> str:
    if not hits:
        return "(no matching constructs in memory)"
    lines = []
    for hit in hits:
        lines.append(
            f"<hit score=\"{hit.get('score')}\" source=\"run {hit.get('run_id')} "
            f"step {hit.get('step')}\" shared=\"{', '.join(hit.get('shared_terms') or [])}\">"
        )
        lines.append(f"  cm1: {', '.join(hit.get('cm1_terms') or [])}")
        lines.append(f"  bridge: {', '.join(hit.get('bridge_terms') or [])}")
        lines.append(f"  cm2: {', '.join(hit.get('cm2_terms') or [])}")
        if hit.get("tension"):
            lines.append(f"  tension: {hit['tension']}")
        if hit.get("rationale"):
            lines.append(f"  rationale: {hit['rationale']}")
        lines.append("</hit>")
    return "\n".join(lines)


def _model_label(model: dict) -> str:
    label = f"{model.get('providerID', '')}/{model.get('id', '')}"
    if model.get("variant"):
        label += f"#{model['variant']}"
    return label


def _stamp(record: dict, job: PilotJob) -> None:
    record["_prompt_version"] = job.prompt_version
    record["_prompt_revision"] = job.prompt_revision
    record["_prompt_comment"] = job.prompt_comment


def new_pilot_output_path() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = config.PILOT_RUNS_DIR / f"{stamp}.jsonl"
    n = 1
    while path.exists():
        n += 1
        path = config.PILOT_RUNS_DIR / f"{stamp}-{n}.jsonl"
    return str(path)
