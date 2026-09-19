"""stats.json + stats.md, computed from a finished job."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .engine import USD_PER_MTOK, JobResult, Prepared
from .spec import ColumnSpec


def build_stats(
    *,
    input_path: Path,
    out_path: Path,
    spec: ColumnSpec,
    model: str,
    endpoint: str,
    prepared: Prepared,
    job: JobResult,
    started_at: float,
    finished_at: float,
) -> dict[str, Any]:
    row_pairs = [(prepared_row, job.results[prepared_row.key]) for prepared_row in prepared.rows]
    error_rows = sum(1 for _, result in row_pairs if result.error is not None)
    review_rows = sum(
        1
        for _, result in row_pairs
        if result.error is None
        and any(
            spec.needs_review(question_id, result.answers.get(question_id) or {})
            for question_id in spec.questions
        )
    )
    automated = len(row_pairs) - error_rows - review_rows
    input_tokens = sum(result.usage.get("input_tokens", 0) for result in job.results.values())
    output_tokens = sum(result.usage.get("output_tokens", 0) for result in job.results.values())
    latencies = [
        result.latency_ms
        for result in job.results.values()
        if result.latency_ms is not None and result.error is None
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input": {"path": str(input_path), "rows": len(row_pairs)},
        "output": str(out_path),
        "spec": {
            "id": spec.id,
            "version": spec.version,
            "license": spec.license,
            "questions": list(spec.questions),
        },
        "model": {
            "requested": model,
            "reported": sorted({result.model for result in job.results.values() if result.model}),
        },
        "endpoint": endpoint,
        "rows": {
            "total": len(row_pairs),
            "unique": len(prepared.unique_keys),
            "calls": job.calls,
            "cache_hits": job.cache_hits,
            "concurrency": job.concurrency,
            "errors": error_rows,
            "review": review_rows,
            "automated": automated,
            "automation_rate": round(automated / len(row_pairs), 4) if row_pairs else 0.0,
        },
        "cost": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "usd": round(input_tokens * USD_PER_MTOK / 1_000_000, 6),
        },
        "latency_ms": {
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
        },
        "runtime_seconds": round(finished_at - started_at, 2),
        "questions": {
            question_id: _question_stats(spec, question_id, row_pairs, error_rows)
            for question_id in spec.questions
        },
    }


def _question_stats(
    spec: ColumnSpec,
    question_id: str,
    row_pairs: list[tuple[Any, Any]],
    error_rows: int,
) -> dict[str, Any]:
    question = spec.questions[question_id]
    answers: list[dict[str, Any]] = []
    review_count = 0
    for _, result in row_pairs:
        if result.error is not None:
            continue
        answer = result.answers.get(question_id) or {}
        answers.append(answer)
        if spec.needs_review(question_id, answer):
            review_count += 1
    stats: dict[str, Any] = {
        "type": question.type,
        "answered": len(answers),
        "review": review_count,
        "error": error_rows,
    }
    if question.type == "choice":
        stats["counts"] = dict(
            Counter(str(answer["choice"]) for answer in answers if answer.get("choice") is not None)
        )
        stats["mean_confidence"] = _mean(
            [answer.get("confidence") for answer in answers if answer.get("confidence") is not None]
        )
    elif question.type == "score":
        stats["counts"] = dict(
            Counter(
                label
                for label, _ in (question.answer_label(answer) for answer in answers)
                if label is not None
            )
        )
        stats["mean_score"] = _mean(
            [answer.get("score") for answer in answers if answer.get("score") is not None]
        )
        stats["mean_confidence"] = _mean(
            [answer.get("confidence") for answer in answers if answer.get("confidence") is not None]
        )
    else:
        stats["mean"] = _mean(
            [answer.get("noul") for answer in answers if answer.get("noul") is not None]
        )
        stats["histogram"] = _histogram(
            [answer["noul"] for answer in answers if answer.get("noul") is not None]
        )
    return stats


def write_stats_json(path: Path, stats: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stats, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_stats_md(path: Path, stats: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(stats), encoding="utf-8")


def render_markdown(stats: dict[str, Any]) -> str:
    rows = stats["rows"]
    cost = stats["cost"]
    latency = stats["latency_ms"]
    models = " → ".join(filter(None, [stats["model"]["requested"], *stats["model"]["reported"]]))
    lines = [
        f"# jev-table — {stats['spec']['id']} v{stats['spec']['version']}",
        "",
        f"- input: `{stats['input']['path']}` ({rows['total']} rows · {rows['unique']} unique)",
        f"- model: `{models}` · endpoint: `{stats['endpoint']}`",
        f"- runtime: {stats['runtime_seconds']} s · {rows['calls']} calls · "
        f"{rows['cache_hits']} from cache",
        f"- cost: {cost['input_tokens']:,} input + {cost['output_tokens']:,} output tokens "
        f"≈ ${cost['usd']:.6f} (output free; includes cached rows)",
        f"- rows: {rows['automated']} automated ({rows['automation_rate'] * 100:.1f}%) · "
        f"{rows['review']} review · {rows['errors']} errors",
    ]
    if latency["p50"] is not None:
        lines.append(f"- latency: p50 {latency['p50']:.0f} ms · p95 {latency['p95']:.0f} ms")
    for question_id, question in stats["questions"].items():
        lines += ["", f"## {question_id} ({question['type']})", ""]
        lines += [
            "| metric | value |",
            "|---|---|",
            f"| answered | {question['answered']} |",
            f"| review | {question['review']} |",
            f"| error | {question['error']} |",
        ]
        if "mean_confidence" in question:
            lines.append(f"| mean confidence | {_fmt(question['mean_confidence'])} |")
        if "mean_score" in question:
            lines.append(f"| mean score | {_fmt(question['mean_score'])} |")
        if "mean" in question:
            lines.append(f"| mean | {_fmt(question['mean'])} |")
        if "counts" in question and question["counts"]:
            lines += ["", "| label | count |", "|---|---|"]
            lines += [f"| {label} | {count} |" for label, count in question["counts"].items()]
        if "histogram" in question:
            lines += ["", "| bucket | count |", "|---|---|"]
            lines += [f"| {bucket} | {count} |" for bucket, count in question["histogram"].items()]
    return "\n".join(lines) + "\n"


def _mean(values: list[Any]) -> float | None:
    clean = [float(value) for value in values if value is not None]
    return round(sum(clean) / len(clean), 4) if clean else None


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(quantile * (len(ordered) - 1))))
    return round(ordered[index], 1)


def _histogram(values: list[float]) -> dict[str, int]:
    buckets = [0] * 10
    for value in values:
        buckets[min(9, max(0, int(float(value) * 10)))] += 1
    return {f"{i / 10:.1f}-{(i + 1) / 10:.1f}": count for i, count in enumerate(buckets)}


def _fmt(value: Any) -> str:
    return "—" if value is None else f"{value:g}"
