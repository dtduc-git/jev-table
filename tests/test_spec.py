from pathlib import Path

import pytest
import yaml
from helpers import write_spec

from jev_table import UsageError
from jev_table.spec import load_column_spec


def _load(tmp_path: Path, **kwargs):
    return load_column_spec(write_spec(tmp_path / "spec", **kwargs))


def test_loads_canonical_spec(tmp_path: Path) -> None:
    spec = _load(tmp_path, thresholds={"queue": {"billing": 0.8}})
    assert spec.id == "test-spec"
    assert spec.version == "0.1.0"
    assert spec.state_fields == ("message",)
    assert set(spec.questions) == {"queue", "urgent", "severity"}
    assert spec.questions["queue"].labels == ("billing", "technical", "unknown")
    assert spec.thresholds == {"queue": {"billing": 0.8}}


def test_api_questions_use_api_shapes(tmp_path: Path) -> None:
    spec = _load(tmp_path)
    api = spec.api_questions()
    assert api["queue"]["criteria"] == {
        "billing": "money",
        "technical": "bugs",
        "unknown": "cannot tell",
    }
    assert api["severity"]["criteria"] == ["low", "high", "unknown"]
    assert api["urgent"] == {"type": "noul", "instructions": "Is it urgent?"}


def test_rejects_wrong_spec_version(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="spec must be 0"):
        _load(tmp_path, spec=1)


def test_rejects_unknown_top_level_key(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="unknown key\\(s\\): gats"):
        _load(tmp_path, gats={})


def test_rejects_choice_without_unknown(tmp_path: Path) -> None:
    questions = {
        "queue": {
            "type": "choice",
            "instructions": "?",
            "options": {"billing": "money", "technical": "bugs"},
        }
    }
    with pytest.raises(UsageError, match="must contain the label 'unknown'"):
        _load(tmp_path, questions=questions)


def test_rejects_single_option_choice(tmp_path: Path) -> None:
    questions = {"queue": {"type": "choice", "instructions": "?", "options": {"unknown": "?"}}}
    with pytest.raises(UsageError, match="at least 2 labels"):
        _load(tmp_path, questions=questions)


def test_rejects_threshold_for_unknown_question(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="not a question id"):
        _load(tmp_path, thresholds={"nope": {"x": 0.5}})


def test_rejects_threshold_for_invalid_label(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="not a valid answer"):
        _load(tmp_path, thresholds={"queue": {"shipping": 0.5}})


def test_rejects_threshold_out_of_range(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match=r"must be in \(0, 1\]"):
        _load(tmp_path, thresholds={"queue": {"billing": 1.5}})


def test_rejects_missing_state_fields(tmp_path: Path) -> None:
    spec_dir = write_spec(tmp_path / "spec")
    raw = yaml.safe_load((spec_dir / "pack.yaml").read_text())
    del raw["state"]["fields"]
    (spec_dir / "pack.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(UsageError, match="state.fields"):
        load_column_spec(spec_dir)


def test_yaml_bool_keys_normalize_to_labels(tmp_path: Path) -> None:
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    (spec_dir / "pack.yaml").write_text(
        "spec: 0\n"
        "id: bool-keys\n"
        "version: 0.1.0\n"
        "license: CC0-1.0\n"
        "description: test\n"
        "state: {fields: [message]}\n"
        "questions:\n"
        "  ok:\n"
        "    type: noul\n"
        '    instructions: "?"\n'
        "thresholds:\n"
        "  ok: {true: 0.9, false: 0.9}\n",
        encoding="utf-8",
    )
    spec = load_column_spec(spec_dir)
    assert spec.thresholds == {"ok": {"true": 0.9, "false": 0.9}}


def test_needs_review_choice(tmp_path: Path) -> None:
    spec = _load(tmp_path, thresholds={"queue": {"billing": 0.8}})
    assert not spec.needs_review("queue", {"choice": "billing", "probabilities": {"billing": 0.9}})
    assert spec.needs_review("queue", {"choice": "billing", "probabilities": {"billing": 0.5}})
    assert spec.needs_review("queue", {"choice": "technical", "probabilities": {"technical": 0.99}})
    assert spec.needs_review("queue", {})


def test_needs_review_without_thresholds_is_always_review(tmp_path: Path) -> None:
    spec = _load(tmp_path)
    assert spec.needs_review("urgent", {"noul": 0.99})


def test_needs_review_noul(tmp_path: Path) -> None:
    spec = _load(tmp_path, thresholds={"urgent": {"true": 0.8, "false": 0.8}})
    assert not spec.needs_review("urgent", {"noul": 0.9})
    assert not spec.needs_review("urgent", {"noul": 0.05})
    assert spec.needs_review("urgent", {"noul": 0.5})


def test_needs_review_score_uses_level_names(tmp_path: Path) -> None:
    spec = _load(tmp_path, thresholds={"severity": {"low": 0.7}})
    assert not spec.needs_review("severity", {"probabilities": {"0": 0.9, "1": 0.05, "2": 0.05}})
    assert spec.needs_review("severity", {"probabilities": {"0": 0.3, "1": 0.6, "2": 0.1}})
    assert spec.needs_review("severity", {"probabilities": {"0": 0.1, "2": 0.9}})
