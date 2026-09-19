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


def _rewrite_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_flywheel_replay_corrections_and_emit_cases(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    strict = {
        "queue": {"billing": 0.95},
        "urgent": {"true": 0.8, "false": 0.8},
        "severity": {"low": 0.7},
    }
    spec_dir, csv_path = _setup(tmp_path, thresholds=strict)
    transports: list[MockTransport] = []

    def factory(args):
        transport = MockTransport()
        transports.append(transport)
        return transport

    monkeypatch.setattr(cli, "_make_transport", factory)
    assert cli.main([str(csv_path), "--spec", str(spec_dir)]) == 0

    from jev_table.output import read_csv as read_csv_file

    corrections_path = tmp_path / "data.jev.corrections.csv"
    fieldnames, rows = read_csv_file(corrections_path)
    assert [row["_row"] for row in rows] == ["0", "2"]  # deduped: "a" appears once
    rows[0]["queue"] = "technical"  # fix a value
    rows[1]["review"] = ""  # confirm row 2 as-is
    _rewrite_csv(corrections_path, fieldnames, rows)

    cases_path = spec_dir / "cases.jsonl"
    code = cli.main(
        [
            str(csv_path),
            "--spec",
            str(spec_dir),
            "--corrections",
            str(corrections_path),
            "--emit-cases",
            str(cases_path),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "applied 2 correction(s)" in out
    assert transports[1].calls == []  # corrected run is all cache hits
    assert not corrections_path.exists()  # all flags resolved

    with (tmp_path / "data.jev.csv").open(newline="", encoding="utf-8") as handle:
        out_rows = list(csv.DictReader(handle))
    assert [row["review"] for row in out_rows] == ["", "", ""]
    assert out_rows[0]["queue"] == "technical"
    assert out_rows[1]["queue"] == "technical"  # duplicate rows share the key
    assert out_rows[2]["queue"] == "billing"

    from jevassert.packs import load_pack

    pack = load_pack(spec_dir)  # emitted cases satisfy the canonical loader
    assert len(pack.cases) == 2
    by_expect = {case.expect["queue"]: case for case in pack.cases}
    assert by_expect["technical"].expect["urgent"] is True
    assert by_expect["technical"].expect["severity"] == "low"
    assert by_expect["billing"].state == {"message": "b"}


def test_replay_corrections_rejects_invalid_label(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    strict = {
        "queue": {"billing": 0.95},
        "urgent": {"true": 0.8, "false": 0.8},
        "severity": {"low": 0.7},
    }
    spec_dir, csv_path = _setup(tmp_path, thresholds=strict)
    monkeypatch.setattr(cli, "_make_transport", lambda args: MockTransport())
    assert cli.main([str(csv_path), "--spec", str(spec_dir)]) == 0

    from jev_table.output import read_csv as read_csv_file

    corrections_path = tmp_path / "data.jev.corrections.csv"
    fieldnames, rows = read_csv_file(corrections_path)
    rows[0]["queue"] = "shipping"
    _rewrite_csv(corrections_path, fieldnames, rows)

    code = cli.main(
        [str(csv_path), "--spec", str(spec_dir), "--corrections", str(corrections_path)]
    )
    assert code == 2
    assert "not one of" in capsys.readouterr().err


def test_emit_cases_requires_corrections(tmp_path: Path, capsys) -> None:
    spec_dir, csv_path = _setup(tmp_path)
    code = cli.main(
        [str(csv_path), "--spec", str(spec_dir), "--emit-cases", str(tmp_path / "cases.jsonl")]
    )
    assert code == 2
    assert "--emit-cases needs --corrections" in capsys.readouterr().err


def test_jsonl_input_end_to_end(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    spec_dir = write_spec(tmp_path / "spec", thresholds=THRESHOLDS)
    path = tmp_path / "data.jsonl"
    path.write_text('{"message": "a", "amount": 12}\n{"message": "b", "amount": 13}\n')
    transport = MockTransport()
    monkeypatch.setattr(cli, "_make_transport", lambda args: transport)
    assert cli.main([str(path), "--spec", str(spec_dir)]) == 0
    assert transport.calls[0][0] == {"message": "a"}  # only spec state fields are sent
    with (tmp_path / "data.jev.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["amount"] for row in rows] == ["12", "13"]
    assert rows[0]["queue"] == "billing"


def test_pinned_model_mismatch_warns(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    spec_dir, csv_path = _setup(tmp_path, rows=[{"message": "a"}, {"message": "b"}])
    monkeypatch.setattr(cli, "_make_transport", lambda args: MockTransport())
    assert cli.main([str(csv_path), "--spec", str(spec_dir), "--model", "jev-1.9.9"]) == 0
    assert "answered by jev-1.13.0" in capsys.readouterr().out


def test_alias_model_never_warns(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    spec_dir, csv_path = _setup(tmp_path, rows=[{"message": "a"}])
    monkeypatch.setattr(cli, "_make_transport", lambda args: MockTransport())
    assert cli.main([str(csv_path), "--spec", str(spec_dir), "--model", "jev-latest"]) == 0
    assert "answered by" not in capsys.readouterr().out


def test_oversize_row_warns_in_dry_run(tmp_path: Path, capsys) -> None:
    spec_dir, csv_path = _setup(tmp_path, rows=[{"message": "x" * 140_000}])
    code = cli.main([str(csv_path), "--spec", str(spec_dir), "--dry-run"])
    assert code == 0
    out = capsys.readouterr().out
    assert "32k-token budget" in out
    assert "estimated total" in out


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
    assert len(rows) == 2  # unique states only: "a" once, "b" once
    assert rows[0]["review"] == "queue"
    assert [row["_row"] for row in rows] == ["0", "2"]
