"""Shared test doubles and builders."""

from __future__ import annotations

import csv
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

DEFAULT_MODEL = "jev-1.13.0"

DEFAULT_QUESTIONS: dict[str, Any] = {
    "queue": {
        "type": "choice",
        "instructions": "Which queue?",
        "options": {"billing": "money", "technical": "bugs", "unknown": "cannot tell"},
    },
    "urgent": {"type": "noul", "instructions": "Is it urgent?"},
    "severity": {
        "type": "score",
        "instructions": "How severe?",
        "levels": ["low", "high", "unknown"],
    },
}


def write_spec(
    parent: Path,
    *,
    questions: dict[str, Any] | None = None,
    thresholds: dict[str, Any] | None = None,
    state_fields: list[str] | None = None,
    **overrides: Any,
) -> Path:
    """Write a spec-compliant pack under ``parent/<id>`` (id must match the dir)."""
    meta: dict[str, Any] = {
        "spec": 0,
        "id": overrides.pop("id", "test-spec"),
        "version": "0.1.0",
        "license": "CC0-1.0",
        "tested": None,
        "description": "Test spec.",
        "state": {"description": "Test state.", "fields": state_fields or ["message"]},
        "questions": questions if questions is not None else DEFAULT_QUESTIONS,
    }
    spec_dir = Path(parent) / meta["id"]
    spec_dir.mkdir(parents=True, exist_ok=True)
    if thresholds is not None:
        meta["thresholds"] = thresholds
    meta.update(overrides)
    (spec_dir / "pack.yaml").write_text(
        yaml.safe_dump(meta, sort_keys=False), encoding="utf-8"
    )
    return spec_dir


def write_csv_file(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path


def payload(
    state: dict[str, str],
    questions: dict[str, dict[str, Any]],
    model: str = DEFAULT_MODEL,
) -> dict[str, Any]:
    """Deterministic answers: first label at 0.9, noul at 0.9."""
    answers: dict[str, dict[str, Any]] = {}
    for question_id, question in questions.items():
        if question["type"] == "choice":
            labels = list(question["criteria"])
            chosen = labels[0]
            probabilities = {label: 0.0 for label in labels}
            probabilities[chosen] = 0.9
            for label in labels[1:]:
                probabilities[label] = 0.1 / (len(labels) - 1)
            answers[question_id] = {
                "type": "choice",
                "choice": chosen,
                "confidence": 0.9,
                "probabilities": probabilities,
            }
        elif question["type"] == "score":
            levels = question["criteria"]
            probabilities = {str(index): 0.0 for index in range(len(levels))}
            probabilities["0"] = 0.9
            for index in range(1, len(levels)):
                probabilities[str(index)] = 0.1 / (len(levels) - 1)
            answers[question_id] = {
                "type": "score",
                "score": 0.0,
                "confidence": 0.9,
                "legend": {str(i): level for i, level in enumerate(levels)},
                "probabilities": probabilities,
            }
        else:
            answers[question_id] = {"type": "noul", "noul": 0.9}
    return {
        "model": model,
        "answers": answers,
        "usage": {"input_tokens": 100, "output_tokens": 10},
    }


class MockTransport:
    """In-memory transport; records calls and answers from a responder."""

    def __init__(self, responder: Callable[..., dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[dict[str, str], dict[str, dict[str, Any]], str]] = []
        self._responder = responder or (lambda state, questions, model: payload(state, questions))

    async def evaluate(
        self, state: dict[str, str], questions: dict[str, dict[str, Any]], model: str
    ) -> dict[str, Any]:
        self.calls.append((state, questions, model))
        return self._responder(state, questions, model)

    async def aclose(self) -> None:
        return None
