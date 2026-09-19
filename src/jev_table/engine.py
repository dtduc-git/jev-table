"""Dedupe, resume cache, concurrency and per-row orchestration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import UsageError
from .spec import ColumnSpec
from .transport import Transport, TransportError

MAX_ROWS_WITHOUT_YES = 10_000
USD_PER_MTOK = 0.042  # input tokens; output tokens are free
DEFAULT_CONCURRENCY = 8
_SOFT_TOKEN_BUDGET_PER_SECOND = 200_000  # documented ceiling is 250k tokens/s
_CONCURRENCY_SAMPLE = 100


@dataclass(frozen=True)
class PreparedRow:
    index: int
    state: dict[str, str]
    key: str


@dataclass
class Prepared:
    rows: list[PreparedRow]
    states: dict[str, dict[str, str]]  # key -> state
    unique_keys: list[str]  # first-seen order


@dataclass
class RowResult:
    key: str
    answers: dict[str, dict[str, Any]] = field(default_factory=dict)
    model: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: float | None = None
    error: str | None = None
    from_cache: bool = False


@dataclass
class JobResult:
    results: dict[str, RowResult]
    calls: int
    cache_hits: int
    concurrency: int


def prepare(rows: list[dict[str, str]], spec: ColumnSpec, model: str) -> Prepared:
    """Map CSV rows to states and deduplicate identical (state, questions, model) rows."""
    if not rows:
        raise UsageError("input has no data rows")
    missing = [name for name in spec.state_fields if name not in rows[0]]
    if missing:
        raise UsageError(
            f"CSV is missing state field(s) from the spec: {', '.join(missing)} "
            f"(found: {', '.join(rows[0])})"
        )
    api_questions = spec.api_questions()
    prepared_rows: list[PreparedRow] = []
    states: dict[str, dict[str, str]] = {}
    unique_keys: list[str] = []
    for index, row in enumerate(rows):
        state = {name: row.get(name) or "" for name in spec.state_fields}
        key = row_key(state, api_questions, model, spec)
        if key not in states:
            states[key] = state
            unique_keys.append(key)
        prepared_rows.append(PreparedRow(index=index, state=state, key=key))
    return Prepared(rows=prepared_rows, states=states, unique_keys=unique_keys)


def row_key(
    state: dict[str, str], api_questions: dict[str, dict[str, Any]], model: str, spec: ColumnSpec
) -> str:
    material = json.dumps(
        {
            "spec": f"{spec.id}@{spec.version}",
            "model": model,
            "state": state,
            "questions": api_questions,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def estimate_tokens(text: str) -> int:
    # ponytail: chars/4 heuristic; Jev's tokenizer is not public
    return max(1, len(text) // 4)


def estimate_call_tokens(state: dict[str, str], api_questions: dict[str, dict[str, Any]]) -> int:
    return estimate_tokens(json.dumps(state, ensure_ascii=False)) + estimate_tokens(
        json.dumps(api_questions, ensure_ascii=False)
    )


def resolve_concurrency(prepared: Prepared, spec: ColumnSpec, requested: int | None) -> int:
    """Concurrency default: 8, auto-capped so in-flight input tokens stay under budget."""
    if requested is not None:
        if requested < 1:
            raise UsageError("--concurrency must be >= 1")
        return requested
    api_questions = spec.api_questions()
    sample = prepared.unique_keys[:_CONCURRENCY_SAMPLE]
    estimates = [estimate_call_tokens(prepared.states[key], api_questions) for key in sample]
    per_call = statistics.median(estimates) if estimates else 1
    cap = max(1, int(_SOFT_TOKEN_BUDGET_PER_SECOND // max(per_call, 1)))
    return max(1, min(DEFAULT_CONCURRENCY, cap))


def load_cache(path: Path) -> dict[str, RowResult]:
    if not path.is_file():
        return {}
    cached: dict[str, RowResult] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue  # ponytail: a torn last line from an interrupted run
        key = raw.get("key")
        if not isinstance(key, str):
            continue
        cached[key] = RowResult(
            key=key,
            answers=raw.get("answers") or {},
            model=raw.get("model"),
            usage=raw.get("usage") or {},
            latency_ms=raw.get("latency_ms"),
            from_cache=True,
        )
    return cached


def append_cache(path: Path, result: RowResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {
            "key": result.key,
            "model": result.model,
            "latency_ms": result.latency_ms,
            "usage": result.usage,
            "answers": result.answers,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()


ProgressCallback = Callable[[int, int, RowResult], None]


async def run(
    prepared: Prepared,
    *,
    spec: ColumnSpec,
    transport: Transport,
    model: str,
    concurrency: int | None = None,
    cache_path: Path | None = None,
    progress: ProgressCallback | None = None,
) -> JobResult:
    """Evaluate every unique row concurrently; resume from cache when present."""
    resolved = resolve_concurrency(prepared, spec, concurrency)
    results: dict[str, RowResult] = {}
    cache_hits = 0
    if cache_path is not None:
        cached = load_cache(cache_path)
        for key in prepared.unique_keys:
            hit = cached.get(key)
            if hit is not None:
                results[key] = hit
                cache_hits += 1
    pending = [key for key in prepared.unique_keys if key not in results]
    api_questions = spec.api_questions()
    semaphore = asyncio.Semaphore(resolved)
    done = 0

    async def evaluate(key: str) -> None:
        nonlocal done
        started = time.perf_counter()
        try:
            async with semaphore:
                payload = await transport.evaluate(prepared.states[key], api_questions, model)
        except TransportError as exc:
            result = RowResult(key=key, error=str(exc), latency_ms=_elapsed_ms(started))
        except Exception as exc:  # ponytail: one bad row never kills the run
            result = RowResult(
                key=key,
                error=f"{type(exc).__name__}: {exc}",
                latency_ms=_elapsed_ms(started),
            )
        else:
            result = RowResult(
                key=key,
                answers=payload.get("answers") or {},
                model=payload.get("model"),
                usage=payload.get("usage") or {},
                latency_ms=_elapsed_ms(started),
            )
        results[key] = result
        if cache_path is not None and result.error is None:
            append_cache(cache_path, result)
        done += 1
        if progress is not None:
            progress(done, len(pending), result)

    if pending:
        await asyncio.gather(*(evaluate(key) for key in pending))
    return JobResult(
        results=results, calls=len(pending), cache_hits=cache_hits, concurrency=resolved
    )


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0
