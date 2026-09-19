"""Column specs are jev-packs ``pack.yaml`` files (format spec v0).

Canonical contract: https://github.com/dtduc-git/jev-packs/blob/main/SPEC.md

A column spec needs only metadata, ``state.fields``, ``questions`` and
``thresholds``; ``cases.jsonl`` / README / CHANGELOG are requirements of the
jev-packs registry, not of classifying unlabeled rows.

Threshold semantics (SPEC.md): a label with a floor is auto-accepted when its
probability reaches the floor. A label with no floor — or a question with no
thresholds at all — cannot be auto-accepted and is routed to review.

TODO: delegate parsing to ``jevassert.packs.load_pack`` once its loader
implements SPEC.md (options/levels, optional cases); today it requires
API-native ``criteria`` questions and a cases.jsonl.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from . import UsageError

SPEC_VERSION = 0
QUESTION_TYPES = ("noul", "choice", "score")
ABSTAIN_LABEL = "unknown"

_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_KEY_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
_TESTED_RE = re.compile(r"^jev-\d+\.\d+\.\d+$")

_TOP_LEVEL_KEYS = {
    "spec",
    "id",
    "version",
    "license",
    "tested",
    "description",
    "state",
    "questions",
    "thresholds",
}
_STATE_KEYS = {"description", "fields"}
_QUESTION_KEYS = {
    "noul": {"type", "instructions"},
    "choice": {"type", "instructions", "options"},
    "score": {"type", "instructions", "levels"},
}


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
    path: Path
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
    """Load a column spec from a pack.yaml file or a directory containing one."""
    source = Path(path)
    spec_file = source / "pack.yaml" if source.is_dir() else source
    if not spec_file.is_file():
        raise UsageError(f"spec not found: {spec_file}")
    try:
        raw = yaml.safe_load(spec_file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise UsageError(f"{spec_file}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise UsageError(f"{spec_file}: top level must be a mapping")

    _reject_unknown_keys(raw, _TOP_LEVEL_KEYS, f"{spec_file}")

    if raw.get("spec") != SPEC_VERSION:
        raise UsageError(f"{spec_file}: spec must be {SPEC_VERSION}, got {raw.get('spec')!r}")

    spec_id = _require_str(raw, "id", spec_file)
    if not _ID_RE.match(spec_id):
        raise UsageError(f"{spec_file}: id must be kebab-case, got {spec_id!r}")
    version = _require_str(raw, "version", spec_file)
    if not _SEMVER_RE.match(version):
        raise UsageError(f"{spec_file}: version must be semver, got {version!r}")
    license_id = _require_str(raw, "license", spec_file)
    description = _require_str(raw, "description", spec_file)

    tested = raw.get("tested")
    if tested is not None:
        if not isinstance(tested, str) or not _TESTED_RE.match(tested):
            raise UsageError(f"{spec_file}: tested must be null or 'jev-<semver>', got {tested!r}")

    state_fields = _parse_state(raw.get("state"), spec_file)
    questions = _parse_questions(raw.get("questions"), spec_file)
    thresholds = _parse_thresholds(raw.get("thresholds"), questions, spec_file)

    return ColumnSpec(
        path=spec_file,
        id=spec_id,
        version=version,
        license=license_id,
        description=description,
        tested=tested,
        state_fields=state_fields,
        questions=questions,
        thresholds=thresholds,
    )


def _parse_state(state: Any, source: Path) -> tuple[str, ...]:
    if not isinstance(state, dict):
        raise UsageError(f"{source}: state must be a mapping with `fields`")
    _reject_unknown_keys(state, _STATE_KEYS, f"{source}: state")
    fields = state.get("fields")
    if not isinstance(fields, list) or not fields:
        raise UsageError(f"{source}: state.fields must be a non-empty list")
    for field in fields:
        if not isinstance(field, str) or not field.strip():
            raise UsageError(f"{source}: state.fields entries must be non-empty strings")
    if len(set(fields)) != len(fields):
        raise UsageError(f"{source}: state.fields contains duplicates")
    return tuple(fields)


def _parse_questions(questions: Any, source: Path) -> dict[str, ColumnQuestion]:
    if not isinstance(questions, dict) or not questions:
        raise UsageError(f"{source}: questions must be a non-empty mapping")
    parsed: dict[str, ColumnQuestion] = {}
    for qid, body in questions.items():
        where = f"{source}: question '{qid}'"
        if not isinstance(qid, str) or not _KEY_RE.match(qid):
            raise UsageError(f"{source}: question id must be snake_case, got {qid!r}")
        if not isinstance(body, dict):
            raise UsageError(f"{where} must be a mapping")
        qtype = body.get("type")
        if qtype not in QUESTION_TYPES:
            raise UsageError(f"{where}: type must be one of {', '.join(QUESTION_TYPES)}")
        _reject_unknown_keys(body, _QUESTION_KEYS[qtype], where)
        instructions = body.get("instructions")
        if not isinstance(instructions, str) or not instructions.strip():
            raise UsageError(f"{where}: instructions must be a non-empty string")

        if qtype == "choice":
            labels = _parse_labels_map(body.get("options"), where, "options")
            api = {
                "type": "choice",
                "instructions": instructions,
                "criteria": {label: description for label, description in labels.items()},
            }
        elif qtype == "score":
            labels = _parse_levels_list(body.get("levels"), where)
            api = {"type": "score", "instructions": instructions, "criteria": list(labels)}
        else:
            labels = ("true", "false")
            api = {"type": "noul", "instructions": instructions}

        parsed[qid] = ColumnQuestion(
            id=qid, type=qtype, instructions=instructions, labels=tuple(labels), api=api
        )
    return parsed


def _label_key(value: Any) -> str | None:
    """YAML 1.1 reads `true`/`yes`/`no` and bare numbers as bool/int keys."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return value if isinstance(value, str) else None


def _parse_labels_map(options: Any, where: str, key: str) -> dict[str, str]:
    if not isinstance(options, dict) or len(options) < 2:
        raise UsageError(f"{where}: {key} must map at least 2 labels to descriptions")
    labels: dict[str, str] = {}
    for label, meaning in options.items():
        normalized = _label_key(label)
        if normalized is None or not _KEY_RE.match(normalized):
            raise UsageError(f"{where}: {key} label must be snake_case, got {label!r}")
        if not isinstance(meaning, str) or not meaning.strip():
            raise UsageError(f"{where}: {key} descriptions must be non-empty strings")
        labels[normalized] = meaning
    if ABSTAIN_LABEL not in labels:
        raise UsageError(
            f"{where}: {key} must contain the label '{ABSTAIN_LABEL}' "
            "(Jev cannot abstain; the question set must offer it — SPEC.md rule 2)"
        )
    return labels


def _parse_levels_list(levels: Any, where: str) -> tuple[str, ...]:
    if not isinstance(levels, list) or not 2 <= len(levels) <= 10:
        raise UsageError(f"{where}: levels must be a list of 2-10 labels")
    normalized: list[str] = []
    for level in levels:
        label = _label_key(level)
        if label is None or not _KEY_RE.match(label):
            raise UsageError(f"{where}: level labels must be snake_case, got {level!r}")
        normalized.append(label)
    if ABSTAIN_LABEL not in normalized:
        raise UsageError(
            f"{where}: levels must contain the label '{ABSTAIN_LABEL}' "
            "(Jev cannot abstain; the question set must offer it — SPEC.md rule 2)"
        )
    return tuple(normalized)


def _parse_thresholds(
    thresholds: Any, questions: dict[str, ColumnQuestion], source: Path
) -> dict[str, dict[str, float]]:
    if thresholds is None:
        return {}
    if not isinstance(thresholds, dict):
        raise UsageError(f"{source}: thresholds must be a mapping of question id -> label floors")
    parsed: dict[str, dict[str, float]] = {}
    for qid, floors in thresholds.items():
        where = f"{source}: thresholds/{qid}"
        question = questions.get(qid)
        if question is None:
            raise UsageError(f"{where}: '{qid}' is not a question id")
        if not isinstance(floors, dict) or not floors:
            raise UsageError(f"{where}: must map label -> probability")
        clean: dict[str, float] = {}
        for raw_label, floor in floors.items():
            label = _label_key(raw_label)
            if label is None or label not in question.labels:
                raise UsageError(
                    f"{where}: '{raw_label}' is not a valid answer for '{qid}' "
                    f"(valid: {', '.join(question.labels)})"
                )
            if isinstance(floor, bool) or not isinstance(floor, int | float):
                raise UsageError(f"{where}: floor for '{label}' must be a number")
            if not 0 < float(floor) <= 1:
                raise UsageError(f"{where}: floor for '{label}' must be in (0, 1]")
            clean[label] = float(floor)
        parsed[qid] = clean
    return parsed


def _require_str(mapping: dict[str, Any], key: str, source: Path) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise UsageError(f"{source}: {key} is required and must be a non-empty string")
    return value


def _reject_unknown_keys(mapping: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise UsageError(f"{where}: unknown key(s): {', '.join(unknown)}")
