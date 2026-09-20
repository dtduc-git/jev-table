#!/bin/bash
# Records docs/demo.gif against the live endpoint (10 rows, ~$0.0002).
# vhs 0.12 cannot launch current Chrome, so this uses asciinema + agg:
#
#   TYPESAFE_API_KEY=... asciinema rec --overwrite --window-size 132x30 \
#     -c "bash docs/demo.sh" /tmp/demo.cast
#   agg --theme dracula --font-size 15 --idle-time-limit 1.2 --speed 1.2 \
#     /tmp/demo.cast docs/demo.gif
#
# The key is read from the environment and never echoed.

: "${TYPESAFE_API_KEY:?export TYPESAFE_API_KEY before recording}"

mkdir -p /tmp/jev-table-demo
cp "$(dirname "$0")/../examples/sms-triage/sample.csv" \
   "$(dirname "$0")/../examples/sms-triage/pack.yaml" /tmp/jev-table-demo/
cd /tmp/jev-table-demo
rm -f ./*.jev.csv ./*.jev.cache.jsonl ./*.jev.corrections.csv ./*.jev.stats.json ./*.jev.stats.md

sleep 0.6
echo '$ ls'
ls
sleep 1.2
echo
echo '$ uvx jev-table sample.csv --spec pack.yaml --dry-run'
uvx jev-table sample.csv --spec pack.yaml --dry-run
sleep 2.2
echo
echo '$ uvx jev-table sample.csv --spec pack.yaml'
uvx jev-table sample.csv --spec pack.yaml
sleep 3.5
echo
echo '$ cut -c1-118 sample.jev.csv | head -6'
cut -c1-118 sample.jev.csv | head -6
sleep 2.5
