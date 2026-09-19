# jev-table — agent notes

Local-first CLI that adds Jev columns to CSVs. Part of the
`jevassert` → `jev-packs` → `jev-table` suite.

## Layout

- `src/jev_table/spec.py` — column spec loader (jev-packs format v0, see
  jev-packs `SPEC.md`) + threshold/review semantics.
- `src/jev_table/engine.py` — dedupe, resume cache, concurrency, per-row
  orchestration. No HTTP here.
- `src/jev_table/transport.py` — the only module that talks to an endpoint
  (typesafe-sdk; mock it in tests).
- `src/jev_table/output.py` — CSV columns, review flags, corrections file.
- `src/jev_table/report.py` — `stats.json` + `stats.md`.
- `src/jev_table/cli.py` — wiring only.
- `tests/helpers.py` — `MockTransport`, pack/CSV builders.

## Commands

```sh
uv sync --all-groups
uv run ruff check .
uv run pytest
uv run jev-table examples/sms-triage/sample.csv --spec examples/sms-triage/pack.yaml --dry-run
```

## Invariants

- Never send row data anywhere except the configured endpoint; no telemetry.
- `check`-style tests must pass without a key: mock the transport, never call
  the live API in tests.
- Review semantics follow jev-packs `SPEC.md`: a label auto-accepts only above
  its floor; no floor (or no thresholds) means review.
- Output columns are a stable interface: input columns + one per question
  (+ `_confidence` for choice/score) + `review` + `error`.
- Spec parsing/validation is delegated to `jevassert.packs`
  (`load_pack(..., require_cases=False)` — table specs are unlabeled). Do not
  add a second parser here.

## Release

Tag `vX.Y.Z` on `master`; the `release.yml` workflow publishes to PyPI via
trusted publishing. Bump `__version__` in `src/jev_table/__init__.py` first.

No external promotion (HN/Reddit/social) without the owner's approval.
