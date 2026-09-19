import asyncio
from pathlib import Path

import pytest
from helpers import MockTransport, payload, write_spec

from jev_table import UsageError
from jev_table.engine import prepare, run
from jev_table.output import (
    answer_columns,
    build_rows,
    output_columns,
    read_csv,
    read_rows,
    write_corrections,
)
from jev_table.spec import load_column_spec
from jev_table.transport import TransportError

THRESHOLDS = {
    "queue": {"billing": 0.8},
    "urgent": {"true": 0.8, "false": 0.8},
    "severity": {"low": 0.7},
}


def responder(state, questions, model):
    if state["message"] == "err":
        raise TransportError("kaboom")
    result = payload(state, questions, model)
    if state["message"] == "uncertain":
        result["answers"]["queue"] = {
            "type": "choice",
            "choice": "billing",
            "confidence": 0.5,
            "probabilities": {"billing": 0.5, "technical": 0.4, "unknown": 0.1},
        }
    return result


def _run(tmp_path: Path, rows: list[dict[str, str]]):
    spec = load_column_spec(write_spec(tmp_path / "spec", thresholds=THRESHOLDS))
    prepared = prepare(rows, spec, "jev-latest")
    job = asyncio.run(
        run(prepared, spec=spec, transport=MockTransport(responder), model="jev-latest")
    )
    return spec, prepared, job


def test_answer_columns_follow_question_types(tmp_path: Path) -> None:
    spec = load_column_spec(write_spec(tmp_path / "spec"))
    assert answer_columns(spec) == [
        "queue",
        "queue_confidence",
        "urgent",
        "severity",
        "severity_confidence",
    ]


def test_read_rows_dispatches_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "data.jsonl"
    path.write_text(
        '{"message": "a", "amount": 12}\n\n{"message": "b", "extra": true}\n',
        encoding="utf-8",
    )
    fieldnames, rows = read_rows(path)
    assert fieldnames == ["message", "amount", "extra"]
    assert rows[0]["amount"] == 12
    assert rows[1]["extra"] is True


def test_read_jsonl_rejects_nested_values(tmp_path: Path) -> None:
    path = tmp_path / "data.jsonl"
    path.write_text('{"message": {"text": "a"}}\n', encoding="utf-8")
    with pytest.raises(UsageError, match="nested values"):
        read_rows(path)


def test_read_jsonl_rejects_non_objects(tmp_path: Path) -> None:
    path = tmp_path / "data.jsonl"
    path.write_text('["a", "b"]\n', encoding="utf-8")
    with pytest.raises(UsageError, match="must be a JSON object"):
        read_rows(path)


def test_read_csv_rejects_ragged_rows(tmp_path: Path) -> None:
    extra = tmp_path / "extra.csv"
    extra.write_text("message\nok,extra\n", encoding="utf-8")
    with pytest.raises(UsageError, match="more fields"):
        read_csv(extra)
    short = tmp_path / "short.csv"
    short.write_text("a,b\nx\n", encoding="utf-8")
    with pytest.raises(UsageError, match="fewer fields"):
        read_csv(short)


def test_output_columns_rejects_clashes(tmp_path: Path) -> None:
    spec = load_column_spec(write_spec(tmp_path / "spec"))
    with pytest.raises(UsageError, match="clash"):
        output_columns(spec, ["message", "queue"])
    with pytest.raises(UsageError, match="clash"):
        output_columns(spec, ["review"])


def test_build_rows_review_and_error(tmp_path: Path) -> None:
    rows = [{"message": "ok"}, {"message": "uncertain"}, {"message": "err"}]
    spec, prepared, job = _run(tmp_path, rows)
    out = build_rows(["message"], rows, prepared, job, spec)
    assert [row["review"] for row in out] == ["", "queue", "queue;urgent;severity"]
    assert out[0]["queue"] == "billing"
    assert out[0]["queue_confidence"] == "0.9"
    assert out[0]["error"] == ""
    assert out[1]["queue_confidence"] == "0.5"
    assert out[2]["error"] == "kaboom"
    assert out[2]["queue"] == ""
    assert out[2]["queue_confidence"] == ""


def test_corrections_hold_only_review_rows_without_errors(tmp_path: Path) -> None:
    rows = [{"message": "ok"}, {"message": "uncertain"}, {"message": "err"}]
    spec, prepared, job = _run(tmp_path, rows)
    out = build_rows(["message"], rows, prepared, job, spec)
    corrections = tmp_path / "corrections.csv"
    count = write_corrections(corrections, ["message"], prepared, out, spec)
    assert count == 1
    fieldnames, data = read_csv(corrections)
    assert fieldnames == [
        "message",
        "_row",
        "queue",
        "queue_confidence",
        "urgent",
        "severity",
        "severity_confidence",
        "review",
    ]
    assert data[0]["_row"] == "1"
    assert [row["message"] for row in data] == ["uncertain"]
    assert data[0]["review"] == "queue"


def test_corrections_not_written_when_nothing_flagged(tmp_path: Path) -> None:
    rows = [{"message": "ok"}]
    spec, prepared, job = _run(tmp_path, rows)
    out = build_rows(["message"], rows, prepared, job, spec)
    assert out[0]["review"] == ""
    corrections = tmp_path / "corrections.csv"
    assert write_corrections(corrections, ["message"], prepared, out, spec) == 0
    assert not corrections.exists()
