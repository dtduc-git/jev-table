import csv
from pathlib import Path

from helpers import MockTransport, write_csv_file, write_spec

import jev_table.cli as cli

THRESHOLDS = {
    "queue": {"billing": 0.8},
    "urgent": {"true": 0.8, "false": 0.8},
    "severity": {"low": 0.7},
}


def _setup(
    tmp_path: Path,
    rows: list[dict[str, str]] | None = None,
    thresholds: dict | None = THRESHOLDS,
    **spec_kwargs,
) -> tuple[Path, Path]:
    spec_dir = write_spec(tmp_path / "spec", thresholds=thresholds, **spec_kwargs)
    csv_path = write_csv_file(
        tmp_path / "data.csv",
        ["message"],
        rows if rows is not None else [{"message": "a"}, {"message": "a"}, {"message": "b"}],
    )
    return spec_dir, csv_path


def test_dry_run_needs_no_key_and_writes_nothing(tmp_path: Path, capsys) -> None:
    spec_dir, csv_path = _setup(tmp_path)
    code = cli.main([str(csv_path), "--spec", str(spec_dir), "--dry-run"])
    assert code == 0
    out = capsys.readouterr().out
    assert "dry run — nothing was sent" in out
    assert "unique: 2" in out
    assert not (tmp_path / "data.jev.csv").exists()


def test_requires_api_key(tmp_path: Path, capsys) -> None:
    spec_dir, csv_path = _setup(tmp_path)
    code = cli.main([str(csv_path), "--spec", str(spec_dir)])
    assert code == 2
    assert "TYPESAFE_API_KEY" in capsys.readouterr().err


def test_large_input_needs_yes(tmp_path: Path, capsys) -> None:
    rows = [{"message": f"m{index}"} for index in range(10_001)]
    spec_dir, csv_path = _setup(tmp_path, rows=rows)
    code = cli.main([str(csv_path), "--spec", str(spec_dir)])
    assert code == 2
    assert "safety cap" in capsys.readouterr().err


def test_large_input_dry_run_allowed(tmp_path: Path, capsys) -> None:
    rows = [{"message": f"m{index}"} for index in range(10_001)]
    spec_dir, csv_path = _setup(tmp_path, rows=rows)
    code = cli.main([str(csv_path), "--spec", str(spec_dir), "--dry-run"])
    assert code == 0
    assert "estimated total" in capsys.readouterr().out


def test_no_thresholds_routes_everything_to_review(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(cli, "_make_transport", lambda args: MockTransport())
    spec_dir = write_spec(tmp_path / "spec")
    csv_path = write_csv_file(tmp_path / "data.csv", ["message"], [{"message": "a"}])
    code = cli.main([str(csv_path), "--spec", str(spec_dir)])
    assert code == 0
    out = capsys.readouterr().out
    assert "always route to review" in out
    assert "review: 1" in out


def test_end_to_end_and_cache_resume(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    spec_dir, csv_path = _setup(tmp_path)
    transports: list[MockTransport] = []

    def factory(args):
        transport = MockTransport()
        transports.append(transport)
        return transport

    monkeypatch.setattr(cli, "_make_transport", factory)

    code = cli.main([str(csv_path), "--spec", str(spec_dir)])
    assert code == 0
    assert len(transports[0].calls) == 2  # deduped: a, a, b

    with (tmp_path / "data.jev.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 3
    assert rows[0]["queue"] == "billing"
    assert rows[0]["queue_confidence"] == "0.9"
    assert rows[0]["urgent"] == "0.9"
    assert rows[0]["severity"] == "0"
    assert rows[0]["review"] == ""
    assert rows[0]["error"] == ""
    assert (tmp_path / "data.jev.stats.json").exists()
    assert (tmp_path / "data.jev.stats.md").exists()
    assert (tmp_path / "data.jev.cache.jsonl").exists()
    assert not (tmp_path / "data.jev.corrections.csv").exists()

    code = cli.main([str(csv_path), "--spec", str(spec_dir)])
    assert code == 0
    assert transports[1].calls == []
    assert "2 from cache" in capsys.readouterr().out


def test_limit(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    spec_dir, csv_path = _setup(tmp_path)
    transport = MockTransport()
    monkeypatch.setattr(cli, "_make_transport", lambda args: transport)
    code = cli.main([str(csv_path), "--spec", str(spec_dir), "--limit", "2"])
    assert code == 0
    assert len(transport.calls) == 1  # first two rows are identical
    with (tmp_path / "data.jev.csv").open(newline="", encoding="utf-8") as handle:
        assert len(list(csv.DictReader(handle))) == 2


def test_corrections_written_for_review_rows(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    strict = {
        "queue": {"billing": 0.95},
        "urgent": {"true": 0.8, "false": 0.8},
        "severity": {"low": 0.7},
    }
    spec_dir, csv_path = _setup(tmp_path, thresholds=strict)
    transport = MockTransport()
    monkeypatch.setattr(cli, "_make_transport", lambda args: transport)
    code = cli.main([str(csv_path), "--spec", str(spec_dir)])
    assert code == 0
    assert "need review" in capsys.readouterr().out
    with (tmp_path / "data.jev.corrections.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 3
    assert rows[0]["review"] == "queue"
