#!/usr/bin/env python3
"""Fetch the UCI SMS Spam Collection and write a small sample CSV.

Dataset: Almeida, T.A., Gomez Hidalgo, J.M., Yamakami, A. (2011), "Contributions
to the Study of SMS Spam Filtering", UCI Machine Learning Repository.
License: CC BY 4.0. Attribution required when you publish results.

Usage:
    python scripts/fetch_sms_spam.py [out.csv] [rows]

Writes `label,message` rows (label = spam|ham) so a live smoke run can be
checked against ground truth. The full collection is never committed.
"""

from __future__ import annotations

import csv
import io
import random
import sys
import urllib.request
import zipfile

URL = "https://archive.ics.uci.edu/static/public/228/sms+spam+collection.zip"


def fetch() -> list[tuple[str, str]]:
    with urllib.request.urlopen(URL, timeout=60) as response:  # noqa: S310
        archive = zipfile.ZipFile(io.BytesIO(response.read()))
    text = archive.read("SMSSpamCollection").decode("utf-8")
    rows: list[tuple[str, str]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        label, _, message = line.partition("\t")
        rows.append((label, message))
    return rows


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else "sms-spam-sample.csv"
    size = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    rows = fetch()
    sample = random.Random(0).sample(rows, min(size, len(rows)))
    with open(out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["label", "message"])
        writer.writerows(sample)
    print(f"wrote {len(sample)} rows to {out} (of {len(rows)} total)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
