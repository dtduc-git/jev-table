# jev-table

[![CI](https://github.com/dtduc-git/jev-table/actions/workflows/ci.yml/badge.svg)](https://github.com/dtduc-git/jev-table/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

AI columns for CSV files, backed by [TypeSafe's Jev](https://typesafe.ai):
label every row with **typed answers, probabilities and a review queue** —
deduplicated, resumable, with a cost preview before you spend anything.

> Independent community tool. Not affiliated with TypeSafe AI.

```sh
uvx jev-table messages.csv --spec pack.yaml --dry-run     # estimate cost, send nothing
uvx jev-table messages.csv --spec pack.yaml               # writes messages.jev.csv + stats
```

| message | category | urgency | mentions_money | review |
|---|---|---|---|---|
| WINNER!! Claim your $500 gift card… | spam | high | 0.99 | |
| Your appointment is confirmed… | ham | normal | 0.02 | |
| [weird message] | | | | category;urgency |

## The column spec is a pack

Columns come from a [jev-packs](https://github.com/dtduc-git/jev-packs) pack
(format [spec v0](https://github.com/dtduc-git/jev-packs/blob/main/SPEC.md)) —
questions as data, no prompts in code:

```yaml
spec: 0
id: sms-triage
version: 0.1.0
license: CC0-1.0
tested: null
description: Label messages as spam/ham and score urgency.
state:
  fields: [message]          # CSV columns sent as the state
questions:
  category:
    type: choice
    instructions: "Is `message` unsolicited commercial spam, or a message the recipient expects?"
    options:
      spam: "Unsolicited bulk or promotional message."
      ham: "Personal, transactional or service message."
      unknown: "Cannot tell from the message alone."
  urgency:
    type: score
    instructions: "How soon does `message` need a human look?"
    levels: [low, normal, high, unknown]
  mentions_money:
    type: noul
    instructions: "Does `message` mention an amount of money or a payment?"
thresholds:
  category: {spam: 0.8, ham: 0.8}
  urgency: {low: 0.7, normal: 0.7, high: 0.7}
  mentions_money: {true: 0.85, false: 0.85}
```

See [`examples/sms-triage/`](examples/sms-triage/) for a runnable pair. Specs
are parsed by the same canonical loader the rest of the suite uses
(`jevassert.packs`); golden cases are optional for table specs.

## Review is the point

A label auto-accepts only when its probability clears its floor in
`thresholds`. Everything else — low probability, a label with no floor (like
`unknown`), or a question with no thresholds at all — lands in the `review`
column:

```
data.jev.csv               # your rows + answers + confidence + review + error
data.jev.corrections.csv   # one row per flagged state (needs a human)
data.jev.stats.json/.md    # cost, latency, automation rate, distributions
data.jev.cache.jsonl       # resume cache (delete to force a full re-run)
```

`review` lists the flagged question ids (`category;urgency`), so you can filter
in any spreadsheet. `category_confidence` and `urgency_confidence` carry the
model's confidence for choice/score answers.

Fix a row by editing its answer cell, or delete the id from `review` to confirm
it as-is, then replay — corrected rows make no API calls and their flags clear:

```sh
jev-table messages.csv --spec pack.yaml \
  --corrections messages.jev.corrections.csv \
  --emit-cases messages.jev.cases.jsonl
```

`--emit-cases` writes the corrected rows as a `cases.jsonl` in jev-packs
format v0: a golden set you can hand to `jevassert record` for evidence. That
is the suite flywheel — jev-table labels, you review, jevassert measures,
jev-packs publishes.

## How it works

- **Dedupe** — identical rows (same state, questions and model) are sent once
  and fanned back out. Duplicates are free, and corrections list one row per
  unique state.
- **All questions, one call per row** — Jev evaluates every question in
  parallel against one state; batching is [an order of magnitude cheaper and
  faster](https://docs.typesafe.ai/cookbooks/parallel_questions) than one call
  per question.
- **Resume** — every successful row is appended to the cache, so a rerun only
  calls what's missing. Errors are never cached and are retried next run.
- **Concurrency** — 8 in-flight requests by default, auto-capped when states
  are large so you stay under the token-rate limit. `--concurrency` overrides.
- **Cost preview** — `--dry-run` estimates tokens (chars/4) across the whole
  input and prints expected dollars at $0.042/Mtok input (output is free);
  every real run also reports actual vs estimated tokens.
- **Safety cap** — files over 10,000 rows need `--yes` after a dry run.

Retries and 429/529 backoff come from the official TypeSafe SDK.

## Privacy

Row data (only the `state.fields` you declared) is sent to the endpoint you
configure — `https://api.typesafe.ai` by default, or anything Jev-compatible
via `--base-url`. Nothing else leaves your machine: no telemetry, no analytics,
no uploads. `--dry-run` sends nothing at all. TypeSafe's
[data handling](https://docs.typesafe.ai/models#data-handling): requests are
not used for training.

## CLI

```
jev-table INPUT --spec SPEC [--out PATH] [--model NAME] [--base-url URL]
           [--limit N] [--concurrency N] [--dry-run]
           [--corrections PATH] [--emit-cases PATH] [--yes] [--no-cache]
```

`INPUT` is a CSV or a flat JSONL (scalar values only; JSONL keeps numbers and
booleans as-is in the state sent to Jev).

| flag | meaning |
|---|---|
| `--spec` | pack.yaml file or its directory (required) |
| `--out` | output CSV (default `<input>.jev.csv`) |
| `--model` | Jev model or alias (default `jev-latest`; pin a version to freeze behavior) |
| `--base-url` | Jev-compatible endpoint (local replicas welcome) |
| `--limit N` | process the first N rows |
| `--dry-run` | estimate tokens/cost; sends nothing, needs no key |
| `--corrections PATH` | replay human edits from a `*.corrections.csv` (needs `_row`) |
| `--emit-cases PATH` | write corrected rows as `cases.jsonl` (jev-packs format v0) |
| `--yes` | allow more than 10,000 rows |
| `--no-cache` | disable the resume cache |

Exit codes: `0` done (review rows are fine), `1` at least one row errored,
`2` bad input or refused run. Requires `TYPESAFE_API_KEY` (any non-empty
value works for local endpoints).

## Non-goals

No web UI, no hosted service, no Excel/PDF (CSV in, CSV out), no auto-written
criteria, no telemetry. Columns come from the spec, never from prompts in code.

## jev-table vs jev-agent-tool

[`jev-agent-tool`](https://github.com/dtduc-git/jev-agent-tool) gives you the
primitive: `evaluate_batch` over 1–50 records. jev-table is the workflow on
top: dedupe, resume, cost preview, review queue, corrections and stats — for
whole files, not batches. If you are writing a script, use the primitive. If
you are labeling a spreadsheet, use jev-table.

## Development

```sh
uv sync --all-groups
uv run ruff check .
uv run pytest
uv run jev-table examples/sms-triage/sample.csv --spec examples/sms-triage/pack.yaml --dry-run
```

`jev-table` imports the pack loader from [`jevassert`](https://github.com/dtduc-git/jevassert)
(`jevassert.packs`). Locally, `uv sync` resolves it from the sibling checkout
via `[tool.uv.sources]`; CI resolves it from PyPI (`uv sync --no-sources`), so
`jevassert` must be published before CI can run.

Live smoke against the public UCI SMS Spam dataset (which ships gold labels):

```sh
python scripts/fetch_sms_spam.py sms-spam-sample.csv 200
TYPESAFE_API_KEY=... uv run jev-table sms-spam-sample.csv --spec examples/sms-triage/pack.yaml
uv run python scripts/report_accuracy.py sms-spam-sample.jev.csv --question category
```

Releases: push a `v*` tag; GitHub Actions builds and publishes to PyPI via
trusted publishing.

## License

Apache-2.0. The SMS Spam Collection dataset is CC BY 4.0 (Almeida et al.,
2011) and fetched, never vendored.
