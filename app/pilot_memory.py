"""Append-only construct memory for the identity pilot.

The pilot deliberately starts with a simple word-overlap retrieval layer. It is
small enough to inspect and replace later (for example, with embeddings or a
BaryGraph vector query) without changing the pilot loop.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Iterable, Optional

from . import config

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'_-]*")
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "because", "been", "being",
    "but", "by", "can", "could", "did", "do", "does", "doing", "for", "from",
    "had", "has", "have", "having", "he", "her", "here", "hers", "him", "his",
    "how", "i", "if", "in", "into", "is", "it", "its", "itself", "just", "me",
    "more", "most", "my", "no", "nor", "not", "now", "of", "on", "once",
    "only", "or", "other", "our", "ours", "out", "over", "own", "same", "she",
    "should", "so", "some", "such", "than", "that", "the", "their", "theirs",
    "them", "then", "there", "these", "they", "this", "those", "through", "to",
    "too", "under", "until", "up", "very", "was", "we", "were", "what", "when",
    "where", "which", "while", "who", "whom", "why", "will", "with", "would",
    "you", "your", "yours", "also", "may", "might", "must", "shall", "still",
    "yet", "about", "above", "after", "again", "against", "all", "am", "any",
    "both", "each", "few", "further", "off", "during", "before", "between",
    "below", "down", "once", "here", "there", "when", "where", "why", "how",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stem(token: str) -> str:
    """Very small English normalizer for word-search matching.

    Keep this conservative. Aggressive suffix stripping produced hits like
    "ongo" for "ongoing", which made the memory prompt look noisier than it was.
    """
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("es"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def normalize_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for match in _TOKEN_RE.findall((text or "").lower()):
        token = _stem(match.strip("_-"))
        if len(token) < 3 or token in _STOPWORDS:
            continue
        if token not in tokens:
            tokens.append(token)
    return tokens


def term_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()] if str(value).strip() else []


def construct_terms(construct: dict) -> list[str]:
    terms: list[str] = []
    for key in ("cm1_terms", "bridge_terms", "cm2_terms"):
        for term in term_list(construct.get(key)):
            for token in normalize_tokens(term):
                if token not in terms:
                    terms.append(token)
    return terms


def construct_weights(construct: dict) -> dict[str, int]:
    weights: dict[str, int] = {}
    weighted = (
        ("cm1_terms", 2),
        ("bridge_terms", 1),
        ("cm2_terms", 2),
    )
    for key, weight in weighted:
        for term in term_list(construct.get(key)):
            for token in normalize_tokens(term):
                weights[token] = max(weights.get(token, 0), weight)
    return weights


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)


def _canonical_key(construct: dict) -> str:
    payload = {
        "cm1": sorted(term_list(construct.get("cm1_terms"))),
        "bridge": sorted(term_list(construct.get("bridge_terms"))),
        "cm2": sorted(term_list(construct.get("cm2_terms"))),
        "tension": _text(construct.get("tension")).strip().lower(),
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def construct_key(construct: dict) -> str:
    """Canonical identity including tension; used for storage and exact dedup."""
    return _canonical_key(construct)


def novelty_key(construct: dict) -> str:
    """Term-set identity ignoring tension; used for the novelty window."""
    payload = {
        "cm1": sorted(term_list(construct.get("cm1_terms"))),
        "bridge": sorted(term_list(construct.get("bridge_terms"))),
        "cm2": sorted(term_list(construct.get("cm2_terms"))),
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _record_id(source: str, construct: dict) -> str:
    raw = f"{source}|{_canonical_key(construct)}".encode("utf-8")
    return "mc_" + hashlib.sha1(raw).hexdigest()[:20]


def make_record(
    construct: dict,
    *,
    run_id: str,
    cycle: int = 1,
    step: int,
    question: str,
    model: str,
    source_excerpt: str = "",
) -> dict:
    source = f"{run_id}|{cycle}|{step}"
    return {
        "id": _record_id(source, construct),
        "created": _now_iso(),
        "run_id": run_id,
        "cycle": cycle,
        "step": step,
        "question": question,
        "model": model,
        "cm1_terms": term_list(construct.get("cm1_terms")),
        "bridge_terms": term_list(construct.get("bridge_terms")),
        "cm2_terms": term_list(construct.get("cm2_terms")),
        "tension": _text(construct.get("tension")) or None,
        "rationale": _text(construct.get("rationale")),
        "source_excerpt": source_excerpt or construct.get("source_excerpt") or "",
        "terms": construct_terms(construct),
    }


def append_records(records: Iterable[dict]) -> int:
    rows = [r for r in records if isinstance(r, dict)]
    if not rows:
        return 0
    config.PILOT_MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(config.PILOT_MEMORY_FILE, "a", encoding="utf-8") as fh:
        for record in rows:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return len(rows)


def read_memory() -> list[dict]:
    if not config.PILOT_MEMORY_FILE.exists():
        return []
    records: list[dict] = []
    for line in config.PILOT_MEMORY_FILE.read_text(encoding="utf-8").splitlines():
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


def memory_stats() -> dict:
    records = read_memory()
    models = sorted({r.get("model") for r in records if r.get("model")})
    return {
        "path": str(config.PILOT_MEMORY_FILE),
        "count": len(records),
        "models": models,
        "last_created": records[-1].get("created") if records else None,
    }


def _fuzzy_overlap(query: dict[str, int], candidate: dict[str, int]) -> dict[str, float]:
    """Exact token matches plus a conservative near-match fallback."""
    matched: dict[str, float] = {
        token: float(weight)
        for token, weight in query.items()
        if token in candidate
    }
    for q_token, q_weight in query.items():
        if q_token in matched:
            continue
        for c_token in candidate:
            if abs(len(q_token) - len(c_token)) > 2:
                continue
            ratio = SequenceMatcher(None, q_token, c_token).ratio()
            if ratio >= 0.88:
                matched[q_token] = q_weight * ratio
                break
    return matched


def _score(
    query_weights: dict[str, int], candidate_weights: dict[str, int]
) -> tuple[float, list[str]]:
    if not query_weights or not candidate_weights:
        return 0.0, []
    matched = _fuzzy_overlap(query_weights, candidate_weights)
    if not matched:
        return 0.0, []
    total = sum(query_weights.values())
    score = sum(matched.values()) / max(1.0, total)
    return score, sorted(matched)


def search_constructs(
    constructs: list[dict],
    *,
    top_k: int = 3,
    exclude_run_id: Optional[str] = None,
    exclude_step: Optional[int] = None,
    exclude_keys: Optional[set[str]] = None,
    min_score: float = 0.0,
    min_shared: int = 0,
    one_per_run: bool = True,
) -> list[dict]:
    """Return the best matching memory records for one or more constructs."""
    query_weights: dict[str, int] = {}
    for construct in constructs:
        for token, weight in construct_weights(construct).items():
            query_weights[token] = max(query_weights.get(token, 0), weight)
    if not query_weights:
        return []

    hits: list[dict] = []
    seen_keys: set[str] = set()
    seen_runs: set[str] = set()
    for record in read_memory():
        if (
            exclude_run_id is not None
            and record.get("run_id") == exclude_run_id
            and record.get("step") == exclude_step
        ):
            continue
        candidate = {
            "cm1_terms": record.get("cm1_terms"),
            "bridge_terms": record.get("bridge_terms"),
            "cm2_terms": record.get("cm2_terms"),
            "tension": record.get("tension"),
        }
        score, shared = _score(query_weights, construct_weights(candidate))
        if score < max(0.0, float(min_score)):
            continue
        if len(shared) < max(0, int(min_shared)):
            continue
        key = _canonical_key(candidate)
        novelty = novelty_key(candidate)
        if exclude_keys and (key in exclude_keys or novelty in exclude_keys):
            continue
        if novelty in seen_keys:
            continue
        run_key = str(record.get("run_id") or "")
        if one_per_run and run_key in seen_runs:
            continue
        seen_keys.add(novelty)
        seen_runs.add(run_key)
        hits.append(
            {
                **record,
                "score": round(score, 4),
                "shared_terms": shared,
            }
        )

    hits.sort(key=lambda h: (-float(h.get("score") or 0), str(h.get("created") or "")))
    return hits[: max(1, int(top_k))]


def seed_from_existing() -> dict:
    """Import existing construct-shaped records into pilot memory.

    Existing Memory Builder runs and examples already contain cm1/bridge/cm2
    records. Importing them gives the pilot a pre-existing memory without
    requiring a special migration UI.
    """
    existing = read_memory()
    existing_ids = {r.get("id") for r in existing if r.get("id")}
    existing_keys = {
        _canonical_key(
            {
                "cm1_terms": r.get("cm1_terms"),
                "bridge_terms": r.get("bridge_terms"),
                "cm2_terms": r.get("cm2_terms"),
                "tension": r.get("tension"),
            }
        )
        for r in existing
    }
    paths = []
    if config.EXAMPLES_FILE.exists():
        paths.append(config.EXAMPLES_FILE)
    paths.extend(sorted(config.RUNS_DIR.glob("*.jsonl")))

    new_records: list[dict] = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            if not any(term_list(record.get(k)) for k in ("cm1_terms", "bridge_terms", "cm2_terms")):
                continue
            construct = {
                "cm1_terms": record.get("cm1_terms"),
                "bridge_terms": record.get("bridge_terms"),
                "cm2_terms": record.get("cm2_terms"),
                "tension": _text(record.get("tension")) or None,
                "rationale": _text(record.get("rationale")),
            }
            key = _canonical_key(construct)
            if key in existing_keys:
                continue
            source = f"seed:{path.name}"
            record_id = _record_id(source, construct)
            if record_id in existing_ids:
                continue
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                created = mtime.isoformat(timespec="seconds")
            except OSError:
                created = _now_iso()
            new_records.append(
                {
                    "id": record_id,
                    "created": created,
                    "run_id": source,
                    "cycle": 0,
                    "step": 0,
                    "question": None,
                    "model": record.get("author") or "seed",
                    "cm1_terms": term_list(record.get("cm1_terms")),
                    "bridge_terms": term_list(record.get("bridge_terms")),
                    "cm2_terms": term_list(record.get("cm2_terms")),
                    "tension": _text(record.get("tension")) or None,
                    "rationale": _text(record.get("rationale")),
                    "source_excerpt": _text(record.get("rationale"))[:300],
                    "terms": construct_terms(construct),
                }
            )
            existing_ids.add(record_id)
            existing_keys.add(key)

    added = append_records(new_records)
    return {
        "ok": True,
        "added": added,
        "scanned_files": len(paths),
        "stats": memory_stats(),
    }


def list_memory(limit: int = 100, query: str = "") -> list[dict]:
    records = read_memory()
    if query.strip():
        q = set(normalize_tokens(query))
        filtered = []
        for record in records:
            terms = set(
                construct_terms(
                    {
                        "cm1_terms": record.get("cm1_terms"),
                        "bridge_terms": record.get("bridge_terms"),
                        "cm2_terms": record.get("cm2_terms"),
                    }
                )
            )
            if q & terms:
                filtered.append(record)
        records = filtered
    return records[-max(1, min(int(limit), 500)) :][::-1]


def clear_memory() -> None:
    config.PILOT_MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.PILOT_MEMORY_FILE.write_text("", encoding="utf-8")


def memory_age_seconds() -> Optional[float]:
    if not config.PILOT_MEMORY_FILE.exists():
        return None
    return max(0.0, time.time() - config.PILOT_MEMORY_FILE.stat().st_mtime)
