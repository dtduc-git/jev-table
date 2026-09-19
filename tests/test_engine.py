import asyncio
from pathlib import Path

import pytest
from helpers import MockTransport, payload, write_spec

from jev_table import UsageError
from jev_table.engine import load_cache, prepare, resolve_concurrency, run
from jev_table.spec import load_column_spec
from jev_table.transport import TransportError

MODEL = "jev-latest"


def _prepared(tmp_path: Path, rows: list[dict[str, str]], **spec_kwargs):
    spec = load_column_spec(write_spec(tmp_path / "spec", **spec_kwargs))
    return spec, prepare(rows, spec, MODEL)


def test_prepare_dedupes_identical_rows(tmp_path: Path) -> None:
    _, prepared = _prepared(tmp_path, [{"message": "a"}, {"message": "a"}, {"message": "b"}])
    assert len(prepared.rows) == 3
    assert len(prepared.unique_keys) == 2
    assert prepared.rows[0].key == prepared.rows[1].key
    assert prepared.rows[0].key != prepared.rows[2].key


def test_prepare_rejects_missing_state_field(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="missing state field"):
        _prepared(tmp_path, [{"other": "a"}])


def test_prepare_rejects_empty_input(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="no data rows"):
        _prepared(tmp_path, [])


def test_prepare_keeps_jsonl_scalar_types(tmp_path: Path) -> None:
    _, prepared = _prepared(
        tmp_path,
        [{"message": "a", "amount": 12, "flagged": True}],
        state_fields=["message", "amount", "flagged"],
    )
    state = prepared.states[prepared.unique_keys[0]]
    assert state == {"message": "a", "amount": 12, "flagged": True}


def test_run_calls_once_per_unique_row(tmp_path: Path) -> None:
    spec, prepared = _prepared(tmp_path, [{"message": "a"}, {"message": "a"}, {"message": "b"}])
    transport = MockTransport()
    job = asyncio.run(run(prepared, spec=spec, transport=transport, model=MODEL))
    assert job.calls == 2
    assert len(transport.calls) == 2
    assert set(job.results) == set(prepared.unique_keys)
    assert all(result.model == "jev-1.13.0" for result in job.results.values())


def test_cache_resumes_without_calls(tmp_path: Path) -> None:
    spec, prepared = _prepared(tmp_path, [{"message": "a"}, {"message": "b"}])
    cache = tmp_path / "cache.jsonl"
    first = asyncio.run(
        run(prepared, spec=spec, transport=MockTransport(), model=MODEL, cache_path=cache)
    )
    assert first.calls == 2
    assert first.cache_hits == 0

    transport = MockTransport()
    second = asyncio.run(
        run(prepared, spec=spec, transport=transport, model=MODEL, cache_path=cache)
    )
    assert second.calls == 0
    assert second.cache_hits == 2
    assert transport.calls == []
    assert all(result.from_cache for result in second.results.values())


def test_errors_are_not_cached_and_are_retried(tmp_path: Path) -> None:
    def responder(state, questions, model):
        if "fail" in state["message"]:
            raise TransportError("boom", status=500)
        return payload(state, questions, model)

    spec, prepared = _prepared(tmp_path, [{"message": "fail"}, {"message": "ok"}])
    cache = tmp_path / "cache.jsonl"
    first = asyncio.run(
        run(
            prepared,
            spec=spec,
            transport=MockTransport(responder),
            model=MODEL,
            cache_path=cache,
        )
    )
    assert first.calls == 2
    failed = [result for result in first.results.values() if result.error]
    assert len(failed) == 1
    assert "boom" in failed[0].error
    assert len(cache.read_text().strip().splitlines()) == 1

    retry = MockTransport(responder)
    second = asyncio.run(run(prepared, spec=spec, transport=retry, model=MODEL, cache_path=cache))
    assert second.calls == 1
    assert second.cache_hits == 1
    assert retry.calls[0][0]["message"] == "fail"


def test_resolve_concurrency_default_and_override(tmp_path: Path) -> None:
    spec, prepared = _prepared(tmp_path, [{"message": "small"}])
    assert resolve_concurrency(prepared, spec, None) == 8
    assert resolve_concurrency(prepared, spec, 3) == 3
    with pytest.raises(UsageError, match=">= 1"):
        resolve_concurrency(prepared, spec, 0)


def test_resolve_concurrency_caps_large_states(tmp_path: Path) -> None:
    spec, prepared = _prepared(tmp_path, [{"message": "x" * 150_000}])
    assert resolve_concurrency(prepared, spec, None) == 5


def test_load_cache_tolerates_torn_line(tmp_path: Path) -> None:
    cache = tmp_path / "cache.jsonl"
    cache.write_text('{"key": "a", "answers": {}, "usage": {}}\n{"key": "b"', encoding="utf-8")
    cached = load_cache(cache)
    assert list(cached) == ["a"]
    assert cached["a"].from_cache
