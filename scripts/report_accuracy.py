#!/usr/bin/env python3
"""Compare jev-table answers against a gold label column.

Usage:
    python scripts/report_accuracy.py out.csv --question category --label-column label

Prints accuracy, the confusion matrix, and the accuracy split between
auto-accepted and review-flagged rows (does the review queue actually catch
the mistakes?). Reads a jev-table output CSV; no dependencies, no network.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out_csv", help="jev-table output CSV")
    parser.add_argument("--question", required=True, help="question column to score")
    parser.add_argument("--label-column", default="label", help="gold label column")
    parser.add_argument("--unknown", default="unknown", help="predicted value to exclude")
    args = parser.parse_args()

    rows: list[tuple[str, str, bool]] = []
    review = errors = 0
    with open(args.out_csv, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        for column in (args.question, args.label_column):
            if column not in fieldnames:
                parser.error(f"column '{column}' not in {args.out_csv}")
        for row in reader:
            if row.get("review"):
                review += 1
            if row.get("error"):
                errors += 1
            gold = (row.get(args.label_column) or "").strip()
            predicted = (row.get(args.question) or "").strip()
            if not gold or not predicted or predicted == args.unknown:
                continue
            rows.append((gold.lower(), predicted.lower(), bool(row.get("review"))))

    if not rows:
        print("no comparable rows (is the label column populated?)")
        return 1
    correct = sum(1 for gold, predicted, _ in rows if gold == predicted)
    print(f"accuracy: {correct}/{len(rows)} = {correct / len(rows):.1%}")
    for label, subset in (
        ("auto", [item for item in rows if not item[2]]),
        ("review", [item for item in rows if item[2]]),
    ):
        hits = sum(1 for gold, predicted, _ in subset if gold == predicted)
        share = f"{hits / len(subset):.1%}" if subset else "—"
        print(f"  {label:>6}: {hits}/{len(subset)} = {share}")

    matrix = Counter((gold, predicted) for gold, predicted, _ in rows)
    labels = sorted({gold for gold, _ in matrix} | {predicted for _, predicted in matrix})
    width = max(9, *(len(label) for label in labels))
    print("\ngold \\ pred".ljust(width) + "".join(label.rjust(width + 1) for label in labels))
    for gold in labels:
        counts = [matrix.get((gold, label), 0) for label in labels]
        print(gold.ljust(width) + "".join(str(count).rjust(width + 1) for count in counts))
    print(f"\nrows: {len(rows)} compared · {review} review · {errors} errors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
