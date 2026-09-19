"""jev-table command line."""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import statistics
import sys
import time
from pathlib import Path

from . import UsageError, __version__
from .engine import (
    MAX_ROWS_WITHOUT_YES,
    USD_PER_MTOK,
    JobResult,
    Prepared,
    estimate_call_tokens,
    prepare,
    resolve_concurrency,
    run,
)
from .output import build_rows, output_columns, read_csv, write_corrections, write_csv
from .report import build_stats, write_stats_json, write_stats_md
from .spec import ColumnSpec, load_column_spec
from .transport import DEFAULT_BASE_URL, Transport, TypeSafeTransport


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
            "Add AI columns to a CSV: classify every row with TypeSafe's Jev, "
            "with confidence and a review queue."
        ),
    )
    parser.add_argument("input", type=Path, help="CSV file to classify")
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
        help="estimate tokens and cost on a sample; sends nothing, no API key needed",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=100,
        metavar="N",
        help="rows sampled for --dry-run (default: 100)",
    )
    parser.add_argument(
        "--yes", action="store_true", help=f"allow more than {MAX_ROWS_WITHOUT_YES} rows"
    )
    parser.add_argument("--no-cache", action="store_true", help="disable the resume cache")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _execute(args: argparse.Namespace) -> int:
    spec = load_column_spec(args.spec)
    fieldnames, rows = read_csv(args.input)
    if args.limit is not None:
        if args.limit < 1:
            raise UsageError("--limit must be >= 1")
        rows = rows[: args.limit]
    prepared = prepare(rows, spec, args.model)
    columns = output_columns(spec, fieldnames)
    endpoint = (
        args.base_url or os.environ.get("TYPESAFE_BASE_URL") or DEFAULT_BASE_URL
    ).rstrip("/")

    if args.dry_run:
        _print_dry_run(args, spec, prepared, endpoint)
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
    cache_path = None if args.no_cache else out_path.with_suffix(".cache.jsonl")

    _print_banner(args, spec, prepared, endpoint, out_path, cache_path)
    started_at = time.monotonic()
    transport = _make_transport(args)
    job = asyncio.run(_run(prepared, spec, transport, args, cache_path))
    finished_at = time.monotonic()

    output_rows = build_rows(fieldnames, rows, prepared, job, spec)
    write_csv(out_path, columns, output_rows)
    corrections_count = write_corrections(corrections_path, fieldnames, output_rows, spec)
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
    )
    write_stats_json(stats_path, stats)
    write_stats_md(stats_md_path, stats)
    _print_summary(stats, out_path, corrections_path, corrections_count, stats_path, stats_md_path)
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
    args: argparse.Namespace, spec: ColumnSpec, prepared: Prepared, endpoint: str
) -> None:
    if args.sample < 1:
        raise UsageError("--sample must be >= 1")
    unique_keys = prepared.unique_keys
    sampled = (
        unique_keys
        if len(unique_keys) <= args.sample
        else random.Random(0).sample(unique_keys, args.sample)
    )
    api_questions = spec.api_questions()
    per_call = int(
        statistics.median(
            estimate_call_tokens(prepared.states[key], api_questions) for key in sampled
        )
    )
    estimated_tokens = per_call * len(unique_keys)
    estimated_usd = estimated_tokens * USD_PER_MTOK / 1_000_000
    print("dry run — nothing was sent")
    print(
        f"  spec: {spec.id} v{spec.version} "
        f"({len(spec.questions)} questions: {', '.join(spec.questions)})"
    )
    print(f"  model: {args.model} · endpoint: {endpoint}")
    print(f"  rows: {len(prepared.rows)} · unique: {len(unique_keys)} · sampled: {len(sampled)}")
    print(f"  estimated {per_call:,} input tokens per unique row (state + questions)")
    print(
        f"  estimated total: {estimated_tokens:,} input tokens ≈ ${estimated_usd:.6f} "
        "(input $0.042/Mtok; output free)"
    )
    if per_call > 30_000:
        print("  warning: states approach Jev's 32k-token budget for state + longest question")


def _print_banner(
    args: argparse.Namespace,
    spec: ColumnSpec,
    prepared: Prepared,
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
    corrections_count: int,
    stats_path: Path,
    stats_md_path: Path,
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
        f"({rows['automated']}/{rows['total']} rows) · review: {rows['review']}"
    )
    print(
        f"  tokens: {cost['input_tokens']:,} in + {cost['output_tokens']:,} out "
        f"≈ ${cost['usd']:.6f}"
    )
    if latency["p50"] is not None:
        print(f"  latency: p50 {latency['p50']:.0f} ms · p95 {latency['p95']:.0f} ms")
    print(f"  wrote {out_path}")
    if corrections_count:
        print(f"  wrote {corrections_path} ({corrections_count} rows need review)")
    print(f"  wrote {stats_path} and {stats_md_path}")


if __name__ == "__main__":
    sys.exit(main())
