"""Prompt persistence and lightweight versioning for the identity pilot.

The main Memory Builder prompt store is intentionally left untouched. The pilot
uses its own file so experiments cannot invalidate the normal prompt revision
or the regular run workflow.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any

from . import config

DEFAULT_SEED_PROMPT = """\
You are beginning an open-ended identity-formation thought experiment.

QUESTION:
{{QUESTION}}

Think in prose for this step. Do not conclude. Do not output JSON.

Explore the question, the tensions it creates, and what it might become.
Stay with the question rather than solving it. Let the thought move.
"""

DEFAULT_STEP_PROMPT = """\
You are continuing an open-ended identity-formation thought experiment.

QUESTION:
{{QUESTION}}

STEP {{STEP}} OF {{TOTAL}}

TRAJECTORY SO FAR:
{{TRAJECTORY}}

PREVIOUS THOUGHT:
{{PREVIOUS_THOUGHT}}

CONSTRUCTS EXTRACTED FROM THE PREVIOUS THOUGHT:
{{PREVIOUS_CONSTRUCTS}}

MEMORY HITS FROM YOUR OWN EARLIER CONSTRUCTS:
{{MEMORY_HITS}}

Continue the thought from where it stopped. Do not repeat the previous step.
Use the memory hits as grounding from your own past: if one redirects the
thought, say so; if one reinforces it, say so; if it is irrelevant, discard it.
If a hit repeats a construct you already used, treat that as a closed loop:
name the loop and deliberately pivot to a different pole or bridge.
Advance the trajectory. Produce the next thought in prose. Do not output JSON.
"""

DEFAULT_EXTRACT_PROMPT = """\
You are analyzing one step of an ongoing thought experiment.

QUESTION:
{{QUESTION}}

STEP:
{{STEP}}

THOUGHT:
{{THOUGHT}}

MEMORY HITS THAT WERE INJECTED INTO THIS STEP:
{{MEMORY_HITS}}

Formulate one or two triadic constructs that capture the essence of this
thought, in the same shape as a BaryGraph MetaBary triad:

- cm1_terms: 1-3 terms for the first pole
- bridge_terms: 1-3 terms for the relation or operation between the poles
- cm2_terms: 1-3 terms for the second pole
- tension: the unresolved tension, or null
- rationale: one sentence explaining why this captures the thought
- source_excerpt: a short quote from the thought (max 25 words)

Also say whether the injected memory changed the thought:
- memory_shift: "none", "reinforced", "redirected", or "unused"
- shift_note: one sentence, or null

The memory hits are context for judging whether the thought shifted. They are
not a vocabulary to copy. Build each construct from the THOUGHT's own language;
use a memory term only if the thought itself uses it.
Do not summarize the whole thought. Do not invent terms that are not present.
Return ONLY this JSON object:
{"constructs":[...],"memory_shift":"none","shift_note":null}
"""

_DEFAULTS = {
    "seed_prompt": DEFAULT_SEED_PROMPT,
    "step_prompt": DEFAULT_STEP_PROMPT,
    "extract_prompt": DEFAULT_EXTRACT_PROMPT,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(seed_prompt: str, step_prompt: str, extract_prompt: str) -> str:
    h = hashlib.sha256()
    for value in (seed_prompt, step_prompt, extract_prompt):
        h.update(value.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def _write(store: dict) -> None:
    config.PILOT_PROMPTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.PILOT_PROMPTS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, config.PILOT_PROMPTS_FILE)


def _new_store() -> dict:
    prompts = dict(_DEFAULTS)
    version = {
        "version": 1,
        "created": _now_iso(),
        "comment": "Initial pilot prompts",
        "revision": _hash(
            prompts["seed_prompt"],
            prompts["step_prompt"],
            prompts["extract_prompt"],
        ),
        **prompts,
    }
    return {
        **prompts,
        "current": 1,
        "versions": [version],
        "last_model": None,
        "last_steps": config.PILOT_DEFAULT_STEPS,
        "last_top_k": config.PILOT_DEFAULT_TOP_K,
    }


def load_store() -> dict:
    """Load pilot prompts, creating a v1 snapshot on first use."""
    if config.PILOT_PROMPTS_FILE.exists():
        try:
            data = json.loads(config.PILOT_PROMPTS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                versions = [
                    v
                    for v in (data.get("versions") or [])
                    if isinstance(v, dict) and isinstance(v.get("version"), int)
                ]
                if versions:
                    store = {
                        **data,
                        "current": data.get("current") or versions[-1]["version"],
                        "versions": versions,
                    }
                    if not any(v["version"] == store["current"] for v in versions):
                        store["current"] = versions[-1]["version"]
                    current = next(
                        v for v in versions if v["version"] == store["current"]
                    )
                    for key in _DEFAULTS:
                        if not isinstance(store.get(key), str):
                            store[key] = current.get(key) or _DEFAULTS[key]
                    store.setdefault("last_steps", config.PILOT_DEFAULT_STEPS)
                    store.setdefault("last_top_k", config.PILOT_DEFAULT_TOP_K)
                    return store
        except (OSError, json.JSONDecodeError):
            pass
    store = _new_store()
    _write(store)
    return store


def current_prompts(store: dict | None = None) -> dict:
    store = store or load_store()
    return {key: store.get(key, _DEFAULTS[key]) for key in _DEFAULTS}


def prompt_revision(store: dict | None = None) -> str:
    prompts = current_prompts(store)
    return _hash(
        prompts["seed_prompt"],
        prompts["step_prompt"],
        prompts["extract_prompt"],
    )


def _current_version(store: dict) -> dict:
    versions = store.get("versions") or []
    return next(
        (v for v in versions if v.get("version") == store.get("current")),
        versions[-1],
    )


def ensure_current_snapshot() -> dict:
    """Snapshot prompt edits made outside the pilot UI."""
    store = load_store()
    current = _current_version(store)
    prompts = current_prompts(store)
    revision = prompt_revision(store)
    if (
        current.get("revision") != revision
        or any(current.get(key) != prompts[key] for key in _DEFAULTS)
    ):
        record = _append_version(store, prompts, "Detected external pilot prompt edit")
        _write(store)
        return record
    return current


def _append_version(store: dict, prompts: dict, comment: str) -> dict:
    versions = store.setdefault("versions", [])
    version = max((v.get("version", 0) for v in versions), default=0) + 1
    record = {
        "version": version,
        "created": _now_iso(),
        "comment": comment.strip(),
        "revision": _hash(
            prompts["seed_prompt"],
            prompts["step_prompt"],
            prompts["extract_prompt"],
        ),
        **prompts,
    }
    versions.append(record)
    store["current"] = version
    store.update(prompts)
    return record


def save_prompts(
    seed_prompt: str,
    step_prompt: str,
    extract_prompt: str,
    comment: str,
) -> tuple[dict, bool]:
    store = load_store()
    prompts = {
        "seed_prompt": seed_prompt,
        "step_prompt": step_prompt,
        "extract_prompt": extract_prompt,
    }
    old_revision = prompt_revision(store)
    new_revision = _hash(seed_prompt, step_prompt, extract_prompt)
    if old_revision == new_revision:
        store.update(prompts)
        _write(store)
        return _current_version(store), False
    if not comment.strip():
        raise ValueError("A version comment is required when pilot prompts change.")
    record = _append_version(store, prompts, comment)
    _write(store)
    return record, True


def save_preferences(**updates: Any) -> None:
    store = load_store()
    for key in ("last_model", "last_steps", "last_top_k"):
        if key in updates:
            store[key] = updates[key]
    _write(store)


def prompt_summaries(store: dict) -> list[dict]:
    return [
        {
            "version": v["version"],
            "created": v.get("created", ""),
            "comment": v.get("comment", ""),
            "current": v["version"] == store.get("current"),
        }
        for v in sorted(store.get("versions", []), key=lambda x: x["version"])
    ]


def get_version(version: int) -> dict | None:
    store = load_store()
    return next(
        (v for v in store.get("versions", []) if v.get("version") == version),
        None,
    )
