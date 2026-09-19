"""Column specs are jev-packs ``pack.yaml`` files (format spec v0).

Parsing and validation are owned by ``jevassert.packs`` — the canonical loader
for the jev-packs format; this module only adapts a validated ``Pack`` to the
table workflow and implements the review semantics from jev-packs ``SPEC.md``.

Threshold semantics: a label with a floor is auto-accepted when its probability
reaches the floor. A label with no floor — or a question with no thresholds at
all — cannot be auto-accepted and is routed to review.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from jevassert.packs import PackError, load_pack

from . import UsageError


@dataclass(frozen=True)
class ColumnQuestion:
    id: str
    type: Literal["noul", "choice", "score"]
    instructions: str
    labels: tuple[str, ...]  # choice options / score levels; noul: ("true", "false")
    api: dict[str, Any]  # the question as sent to POST /v1/systemone

    def answer_label(self, answer: dict[str, Any]) -> tuple[str | None, float]:
        """The answer's label and its probability, or (None, 0.0) if unusable."""
        if self.type == "choice":
            label = answer.get("choice")
            probabilities = answer.get("probabilities") or {}
            if not isinstance(label, str):
                return None, 0.0
            return label, float(probabilities.get(label, 0.0))
        if self.type == "score":
            probabilities = {
                str(key): float(value) for key, value in (answer.get("probabilities") or {}).items()
            }
            if not probabilities:
                return None, 0.0
            index = max(probabilities, key=probabilities.__getitem__)
            try:
                return str(self.labels[int(index)]), probabilities[index]
            except (ValueError, IndexError):
                return None, 0.0
        probability = float(answer.get("noul", 0.0))
        if probability >= 0.5:
            return "true", probability
        return "false", 1.0 - probability


@dataclass(frozen=True)
class ColumnSpec:
    id: str
    version: str
    license: str
    description: str
    tested: str | None
    state_fields: tuple[str, ...]
    questions: dict[str, ColumnQuestion]
    thresholds: dict[str, dict[str, float]]

    def api_questions(self) -> dict[str, dict[str, Any]]:
        return {qid: dict(question.api) for qid, question in self.questions.items()}

    def needs_review(self, question_id: str, answer: dict[str, Any]) -> bool:
        """SPEC.md operating point: a label auto-accepts only above its floor."""
        floors = self.thresholds.get(question_id)
        if not floors:
            return True
        label, probability = self.questions[question_id].answer_label(answer)
        if label is None:
            return True
        floor = floors.get(label)
        return floor is None or probability < floor


def load_column_spec(path: str | Path) -> ColumnSpec:
    """Load a column spec from a pack.yaml file or a directory containing one.

    Golden cases are optional for table specs: rows to classify are unlabeled.
    """
    try:
        pack = load_pack(path, require_cases=False)
    except PackError as exc:
        raise UsageError(str(exc)) from exc
    api_questions = pack.to_api_questions()
    questions = {
        qid: ColumnQuestion(
            id=qid,
            type=raw["type"],
            instructions=raw["instructions"],
            labels=_labels(raw),
            api=api_questions[qid],
        )
        for qid, raw in pack.questions.items()
    }
    return ColumnSpec(
        id=pack.id,
        version=pack.version,
        license=pack.license,
        description=pack.description,
        tested=pack.tested,
        state_fields=tuple(pack.state["fields"]),
        questions=questions,
        thresholds=pack.thresholds,
    )


def _labels(question: dict[str, Any]) -> tuple[str, ...]:
    if question["type"] == "noul":
        return ("true", "false")
    if question["type"] == "choice":
        return tuple(question["options"])
    return tuple(question["levels"])
