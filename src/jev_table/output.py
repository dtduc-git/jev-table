"""CSV in, CSV out: answer columns, review flags, corrections file."""

from __future__ import annotations

import csv
from collections.abc import Iterable, Sequence
from pathlib import Path

from . import UsageError
from .engine import JobResult, Prepared
from .spec import ColumnQuestion, ColumnSpec

RESERVED_COLUMNS = ("review", "error")


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.is_file():
        raise UsageError(f"input file not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise UsageError(f"{path}: no header row")
        fieldnames = list(reader.fieldnames)
        rows: list[dict[str, str]] = []
        for raw in reader:
            if None in raw:
                raise UsageError(
                    f"{path}: row {reader.line_num} has more fields than the header "
                    "(quote fields that contain the delimiter)"
                )
            if any(raw.get(name) is None for name in fieldnames):
                raise UsageError(
                    f"{path}: row {reader.line_num} has fewer fields than the header"
                )
            rows.append({name: (raw.get(name) or "") for name in fieldnames})
    return fieldnames, rows


def answer_columns(spec: ColumnSpec) -> list[str]:
    columns: list[str] = []
    for question_id, question in spec.questions.items():
        columns.append(question_id)
        if question.type in ("choice", "score"):
            columns.append(f"{question_id}_confidence")
    return columns


def output_columns(spec: ColumnSpec, input_fields: Sequence[str]) -> list[str]:
    generated = answer_columns(spec) + list(RESERVED_COLUMNS)
    if len(set(generated)) != len(generated):
        raise UsageError("spec generates duplicate columns — rename a question id")
    clashes = sorted(set(generated) & set(input_fields))
    if clashes:
        raise UsageError(
            "generated column(s) clash with input columns: "
            + ", ".join(clashes)
            + " — rename them in the CSV or the spec"
        )
    return list(input_fields) + generated


def build_rows(
    fieldnames: Sequence[str],
    rows: list[dict[str, str]],
    prepared: Prepared,
    job: JobResult,
    spec: ColumnSpec,
) -> list[dict[str, str]]:
    """One output row per input row, answers fanned out to duplicates."""
    output: list[dict[str, str]] = []
    for input_row, prepared_row in zip(rows, prepared.rows, strict=True):
        result = job.results[prepared_row.key]
        out_row = {name: input_row.get(name, "") for name in fieldnames}
        review: list[str] = []
        if result.error is not None:
            review = list(spec.questions)
            for question_id, question in spec.questions.items():
                out_row[question_id] = ""
                if question.type in ("choice", "score"):
                    out_row[f"{question_id}_confidence"] = ""
        else:
            for question_id, question in spec.questions.items():
                answer = result.answers.get(question_id) or {}
                value, confidence = format_answer(question, answer)
                out_row[question_id] = value
                if question.type in ("choice", "score"):
                    out_row[f"{question_id}_confidence"] = confidence
                if spec.needs_review(question_id, answer):
                    review.append(question_id)
        out_row["review"] = ";".join(review)
        out_row["error"] = result.error or ""
        output.append(out_row)
    return output


def format_answer(question: ColumnQuestion, answer: dict[str, object]) -> tuple[str, str]:
    if not answer:
        return "", ""
    if question.type == "choice":
        return _text(answer.get("choice")), _number(answer.get("confidence"))
    if question.type == "score":
        return _number(answer.get("score")), _number(answer.get("confidence"))
    return _number(answer.get("noul")), ""


def write_csv(path: Path, fieldnames: Sequence[str], rows: Iterable[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_corrections(
    path: Path,
    fieldnames: Sequence[str],
    output_rows: list[dict[str, str]],
    spec: ColumnSpec,
) -> int:
    """Rows a human must look at: review-flagged, no error. Returns the row count."""
    columns = list(fieldnames) + answer_columns(spec) + ["review"]
    flagged = [row for row in output_rows if row["review"] and not row["error"]]
    if not flagged:
        return 0
    write_csv(path, columns, flagged)
    return len(flagged)


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _number(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int | float):
        return f"{value:.6g}"
    return str(value)
