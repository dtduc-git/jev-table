"""Replay human corrections and emit golden cases — the jev-table flywheel.

Workflow: run once, open ``*.corrections.csv``, fix the flagged answers, rerun
with ``--corrections`` (no API calls for corrected rows), then ``--emit-cases``
to produce a ``cases.jsonl`` the jev-packs/jevassert suite can consume.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from . import UsageError
from .engine import JobResult, Prepared, RowResult, append_cache
from .output import ROW_COLUMN
from .spec import ColumnQuestion, ColumnSpec

_TRUE_WORDS = {"true", "yes", "1"}
_FALSE_WORDS = {"false", "no", "0"}


def read_corrections(path: Path) -> dict[int, dict[str, str]]:
    """Map input row index -> raw corrected cells; requires the `_row` column."""
    if not path.is_file():
        raise UsageError(f"corrections file not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or ROW_COLUMN not in reader.fieldnames:
            raise UsageError(f"{path}: corrections file needs a `{ROW_COLUMN}` column")
        corrections: dict[int, dict[str, str]] = {}
        for raw in reader:
            try:
                index = int(raw[ROW_COLUMN])
            except (TypeError, ValueError):
                raise UsageError(
                    f"{path}: invalid {ROW_COLUMN} value {raw[ROW_COLUMN]!r}"
                ) from None
            if index in corrections:
                raise UsageError(f"{path}: duplicate {ROW_COLUMN} {index}")
            corrections[index] = {key: value for key, value in raw.items() if value}
    return corrections


def apply_corrections(
    corrections: dict[int, dict[str, str]],
    *,
    prepared: Prepared,
    job: JobResult,
    spec: ColumnSpec,
    cache_path: Path | None = None,
) -> int:
    """Override answers with human-corrected cells; mark questions as verified.

    A question is verified when its cell was edited, or when the user removed
    its id from the row's `review` column (confirm-as-is). Returns the number
    of applied corrections.
    """
    applied = 0
    processed: set[str] = set()
    for index, cells in corrections.items():
        if not 0 <= index < len(prepared.rows):
            raise UsageError(
                f"corrections reference row {index}, but the input has "
                f"{len(prepared.rows)} row(s) (0..{len(prepared.rows) - 1})"
            )
        key = prepared.rows[index].key
        if key in processed:
            continue  # duplicate states share one result; first occurrence wins
        processed.add(key)
        result = job.results[key]
        cells = dict(cells)
        still_in_review = {
            qid.strip() for qid in cells.pop("review", "").split(";") if qid.strip()
        }
        for question_id, raw in cells.items():
            question = spec.questions.get(question_id)
            if question is None:
                continue  # unknown column: ignore (the file may carry extra columns)
            original = result.answers.get(question_id) or {}
            corrected = _corrected_answer(question, raw.strip(), original)
            if corrected is original:
                continue  # cell left as-is
            result.answers[question_id] = corrected
            _verify(result, question_id)
            applied += 1
        for question_id in spec.questions:
            if question_id in still_in_review:
                continue
            answer = result.answers.get(question_id)
            if answer and question_id not in result.verified:
                if spec.needs_review(question_id, answer):
                    _verify(result, question_id)  # removed from review: confirmed as-is
                    applied += 1
        if result.verified and cache_path is not None:
            append_cache(cache_path, result)
    return applied


def _verify(result: RowResult, question_id: str) -> None:
    if question_id not in result.verified:
        result.verified.append(question_id)


def emit_cases(
    path: Path,
    *,
    prepared: Prepared,
    job: JobResult,
    spec: ColumnSpec,
) -> int:
    """Write cases.jsonl (SPEC v0) for rows with at least one verified answer."""
    seen: set[str] = set()
    lines: list[str] = []
    for prepared_row in prepared.rows:
        result = job.results[prepared_row.key]
        if prepared_row.key in seen or not result.verified:
            continue
        expect: dict[str, Any] = {}
        for question_id, question in spec.questions.items():
            label = _expected_label(question, result)
            if label is None:
                expect = {}
                break
            expect[question_id] = label
        if not expect:
            continue
        seen.add(prepared_row.key)
        lines.append(
            json.dumps(
                {
                    "id": f"{spec.id}-{prepared_row.index + 1:05d}",
                    "state": prepared_row.state,
                    "expect": expect,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    if not lines:
        raise UsageError(
            "nothing to emit: no corrected rows with complete answers "
            "(run with --corrections first)"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


def _corrected_answer(
    question: ColumnQuestion, raw: str, original: dict[str, Any]
) -> dict[str, Any]:
    if not raw:
        return original
    where = f"corrections: question '{question.id}'"
    if question.type == "choice":
        if raw not in question.labels:
            raise UsageError(f"{where}: '{raw}' is not one of {', '.join(question.labels)}")
        if original.get("choice") == raw:
            return original
        return {
            "type": "choice",
            "choice": raw,
            "confidence": 1.0,
            "probabilities": {raw: 1.0},
        }
    if question.type == "score":
        if raw in question.labels:
            value = float(question.labels.index(raw))
        else:
            try:
                value = float(raw)
            except ValueError:
                raise UsageError(
                    f"{where}: '{raw}' is neither a number nor one of "
                    f"{', '.join(question.labels)}"
                ) from None
        if original.get("score") == value:
            return original
        index = max(0, min(len(question.labels) - 1, int(round(value))))
        return {
            "type": "score",
            "score": value,
            "confidence": 1.0,
            "probabilities": {str(index): 1.0},
        }
    value = _noul_value(raw, where)
    if original.get("noul") == value:
        return original
    return {"type": "noul", "noul": value}


def _noul_value(raw: str, where: str) -> float:
    lowered = raw.lower()
    if lowered in _TRUE_WORDS:
        return 1.0
    if lowered in _FALSE_WORDS:
        return 0.0
    try:
        value = float(raw)
    except ValueError:
        raise UsageError(f"{where}: '{raw}' is not a probability in [0, 1] or true/false") from None
    if not 0.0 <= value <= 1.0:
        raise UsageError(f"{where}: '{raw}' is not a probability in [0, 1]")
    return value


def _expected_label(question: ColumnQuestion, result: RowResult) -> Any:
    answer = result.answers.get(question.id) or {}
    if question.type == "choice":
        choice = answer.get("choice")
        return choice if choice in question.labels else None
    if question.type == "score":
        score = answer.get("score")
        if score is None:
            return None
        index = max(0, min(len(question.labels) - 1, int(round(float(score)))))
        return question.labels[index]
    probability = answer.get("noul")
    return None if probability is None else bool(float(probability) >= 0.5)
