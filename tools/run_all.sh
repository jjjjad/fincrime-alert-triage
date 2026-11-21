#!/usr/bin/env bash
# Runs the system on all four institutions and validates each output.
# Run from the repo root, with the hackathon's institutions/ folder copied in (it is gitignored).
cd "$(dirname "$0")/.."
for inst in bank emoney investment payments_fx; do
    time system/run "institutions/$inst" "output/$inst.jsonl"
    python3 tools/validate.py "institutions/$inst" "output/$inst.jsonl"
done
