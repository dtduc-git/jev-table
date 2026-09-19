"""jev-table command line."""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time
from pathlib import Path

from . import UsageError, __version__
from .corrections import apply_corrections, emit_cases, read_corrections
from .engine import (
    MAX_ROWS_WITHOUT_YES,
    USD_PER_MTOK,
    JobResult,
    Prepared,
    estimate_tokens_per_row,
    prepare,
    resolve_concurrency,
    run,
)
from .output import build_rows, output_columns, read_rows, write_corrections, write_csv
from .report import build_stats, write_stats_json, write_stats_md
from .spec import ColumnSpec, load_column_spec
from .transport import DEFAULT_BASE_URL, Transport, TypeSafeTransport

_OVERSIZE_TOKENS = 30_000  # 32k budget for state + longest question, with headroom
_ALIASES = ("jev-latest", "jev-preview")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return _execute(args)
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jev-table",
        description=(
            "Add AI columns to a CSV or JSONL: classify every row with TypeSafe's Jev, "
            "with confidence and a review queue."
        ),
    )
    parser.add_argument("input", type=Path, help="CSV or JSONL file to classify")
    parser.add_argument(
        "--spec",
        required=True,
        type=Path,
        help="column spec: a pack.yaml file or a directory containing one",
    )
    parser.add_argument(
        "--out", type=Path, default=None, help="output CSV (default: <input stem>.jev.csv)"
    )
    parser.add_argument("--model", default="jev-latest", help="Jev model or alias")
    parser.add_argument(
        "--base-url",
        default=None,
        help=f"Jev-compatible endpoint (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--limit", type=int, default=None, metavar="N", help="process only the first N rows"
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        metavar="N",
        help="max in-flight requests (default: 8, auto-capped for large states)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="estimate tokens and cost; sends nothing, no API key needed",
    )
    parser.add_argument(
        "--yes", action="store_true", help=f"allow more than {MAX_ROWS_WITHOUT_YES} rows"
    )
    parser.add_argument("--no-cache", action="store_true", help="disable the resume cache")
    parser.add_argument(
        "--corrections",
        type=Path,
        default=None,
        metavar="PATH",
        help="replay human corrections from a *.corrections.csv file (needs the _row column)",
    )
    parser.add_argument(
        "--emit-cases",
        type=Path,
        default=None,
        metavar="PATH",
        help="write cases.jsonl (jev-packs format v0) for rows corrected by hand",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _execute(args: argparse.Namespace) -> int:
    spec = load_column_spec(args.spec)
    if args.emit_cases and not args.corrections:
        raise UsageError("--emit-cases needs --corrections (there is nothing to emit yet)")
    fieldnames, rows = read_rows(args.input)
    if args.limit is not None:
        if args.limit < 1:
            raise UsageError("--limit must be >= 1")
        rows = rows[: args.limit]
    prepared = prepare(rows, spec, args.model)
    columns = output_columns(spec, fieldnames)
    endpoint = (
        args.base_url or os.environ.get("TYPESAFE_BASE_URL") or DEFAULT_BASE_URL
    ).rstrip("/")
    estimates = estimate_tokens_per_row(prepared, spec)

    if args.dry_run:
        _print_dry_run(args, spec, prepared, estimates, endpoint)
        return 0

    if len(rows) > MAX_ROWS_WITHOUT_YES and not args.yes:
        raise UsageError(
            f"{len(rows)} rows exceeds the {MAX_ROWS_WITHOUT_YES}-row safety cap; "
            "preview with --dry-run, then pass --yes to run"
        )

    if not os.environ.get("TYPESAFE_API_KEY", "").strip():
        raise UsageError(
            "TYPESAFE_API_KEY is not set (any non-empty value works for local endpoints)"
        )

    out_path = args.out or args.input.with_suffix(".jev.csv")
    stats_path = out_path.with_suffix(".stats.json")
    stats_md_path = out_path.with_suffix(".stats.md")
    corrections_path = out_path.with_suffix(".corrections.csv")
    cases_path = args.emit_cases
    cache_path = None if args.no_cache else out_path.with_suffix(".cache.jsonl")

    _print_banner(args, spec, prepared, estimates, endpoint, out_path, cache_path)
    started_at = time.monotonic()
    transport = _make_transport(args)
    job = asyncio.run(_run(prepared, spec, transport, args, cache_path))
    finished_at = time.monotonic()

    if args.corrections is not None:
        corrected_cells = apply_corrections(
            read_corrections(args.corrections),
            prepared=prepared,
            job=job,
            spec=spec,
            cache_path=cache_path,
        )
        print(f"  applied {corrected_cells} correction(s) from {args.corrections}")

    output_rows = build_rows(fieldnames, rows, prepared, job, spec)
    write_csv(out_path, columns, output_rows)
    review_count = write_corrections(corrections_path, fieldnames, prepared, output_rows, spec)
    stats = build_stats(
        input_path=args.input,
        out_path=out_path,
        spec=spec,
        model=args.model,
        endpoint=endpoint,
        prepared=prepared,
        job=job,
        started_at=started_at,
        finished_at=finished_at,
        estimated_input_tokens=sum(estimates),
    )
    write_stats_json(stats_path, stats)
    write_stats_md(stats_md_path, stats)
    emitted = 0
    if cases_path is not None:
        emitted = emit_cases(cases_path, prepared=prepared, job=job, spec=spec)
    _print_summary(
        stats,
        out_path,
        corrections_path,
        review_count,
        stats_path,
        stats_md_path,
        cases_path,
        emitted,
    )
    return 1 if stats["rows"]["errors"] else 0


async def _run(
    prepared: Prepared,
    spec: ColumnSpec,
    transport: Transport,
    args: argparse.Namespace,
    cache_path: Path | None,
) -> JobResult:
    try:
        return await run(
            prepared,
            spec=spec,
            transport=transport,
            model=args.model,
            concurrency=args.concurrency,
            cache_path=cache_path,
            progress=_progress_printer(),
        )
    finally:
        await transport.aclose()


def _make_transport(args: argparse.Namespace) -> Transport:
    return TypeSafeTransport(base_url=args.base_url)


def _print_dry_run(
    args: argparse.Namespace,
    spec: ColumnSpec,
    prepared: Prepared,
    estimates: list[int],
    endpoint: str,
) -> None:
    total = sum(estimates)
    per_row = int(statistics.median(estimates)) if estimates else 0
    estimated_usd = total * USD_PER_MTOK / 1_000_000
    print("dry run — nothing was sent")
    print(
        f"  spec: {spec.id} v{spec.version} "
        f"({len(spec.questions)} questions: {', '.join(spec.questions)})"
    )
    print(f"  model: {args.model} · endpoint: {endpoint}")
    print(f"  rows: {len(prepared.rows)} · unique: {len(prepared.unique_keys)}")
    print(f"  estimated {per_row:,} input tokens per unique row (state + questions)")
    print(
        f"  estimated total: {total:,} input tokens ≈ ${estimated_usd:.6f} "
        "(input $0.042/Mtok; output free)"
    )
    oversize = sum(1 for tokens in estimates if tokens > _OVERSIZE_TOKENS)
    if oversize:
        print(
            f"  warning: {oversize} row(s) near Jev's 32k-token budget for "
            "state + longest question; split or shorten them"
        )


def _print_banner(
    args: argparse.Namespace,
    spec: ColumnSpec,
    prepared: Prepared,
    estimates: list[int],
    endpoint: str,
    out_path: Path,
    cache_path: Path | None,
) -> None:
    concurrency = resolve_concurrency(prepared, spec, args.concurrency)
    print(f"jev-table {__version__} — spec {spec.id} v{spec.version} · model {args.model}")
    print(f"  endpoint: {endpoint}")
    print(
        "  row data (the state fields) is sent to that endpoint; "
        "nothing else leaves this machine."
    )
    print(
        f"  rows: {len(prepared.rows)} · unique: {len(prepared.unique_keys)} · "
        f"concurrency: {concurrency}"
    )
    ungated = [qid for qid in spec.questions if qid not in spec.thresholds]
    if ungated:
        print(
            f"  note: no thresholds for {', '.join(ungated)} — "
            "those answers always route to review"
        )
    oversize = sum(1 for tokens in estimates if tokens > _OVERSIZE_TOKENS)
    if oversize:
        print(f"  warning: {oversize} row(s) may exceed Jev's 32k-token budget")
    print(f"  out: {out_path} · cache: {'off' if cache_path is None else cache_path}")


def _progress_printer():
    tty = sys.stdout.isatty()

    def on_progress(done: int, total: int, result) -> None:  # noqa: ANN001
        if tty:
            print(f"\r  {done}/{total} rows evaluated", end="", flush=True)
            if done == total:
                print()
        elif done % 25 == 0 or done == total:
            print(f"  {done}/{total} rows evaluated", flush=True)

    return on_progress


def _print_summary(
    stats: dict,
    out_path: Path,
    corrections_path: Path,
    review_count: int,
    stats_path: Path,
    stats_md_path: Path,
    cases_path: Path | None,
    emitted: int,
) -> None:
    rows = stats["rows"]
    cost = stats["cost"]
    latency = stats["latency_ms"]
    print(
        f"done: {rows['calls']} calls · {rows['cache_hits']} from cache · "
        f"{rows['errors']} errors"
    )
    print(
        f"  automation: {rows['automation_rate'] * 100:.1f}% "
        f"({rows['automated']}/{rows['total']} rows) · review: {rows['review']} · "
        f"verified: {rows['verified']}"
    )
    estimated = cost["estimated_input_tokens"]
    delta = ""
    if estimated:
        delta = f" (estimated {estimated:,}, {cost['input_tokens'] / estimated:+.2f}x)"
    print(
        f"  tokens: {cost['input_tokens']:,} in + {cost['output_tokens']:,} out "
        f"≈ ${cost['usd']:.6f}{delta}"
    )
    if latency["p50"] is not None:
        print(f"  latency: p50 {latency['p50']:.0f} ms · p95 {latency['p95']:.0f} ms")
    reported = stats["model"]["reported"]
    requested = stats["model"]["requested"]
    if reported and requested not in _ALIASES:
        unexpected = [model for model in reported if model != requested]
        if unexpected:
            print(f"  warning: pinned model {requested} was answered by {', '.join(unexpected)}")
    print(f"  wrote {out_path}")
    if review_count:
        print(f"  wrote {corrections_path} ({review_count} rows need review)")
    if cases_path is not None:
        print(f"  wrote {cases_path} ({emitted} golden cases)")
    print(f"  wrote {stats_path} and {stats_md_path}")


if __name__ == "__main__":
    sys.exit(main())
