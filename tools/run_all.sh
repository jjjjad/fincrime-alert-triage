#!/usr/bin/env bash
# Run the system on every institution folder, validate, and check that a
# second run is byte-identical (the brief scores reproducibility over 5 runs).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="${1:-$ROOT/institutions}"
OUT="${2:-$ROOT/output}"
mkdir -p "$OUT"
for dir in "$DATA"/*/; do
  name="$(basename "$dir")"
  [ -f "$dir/cases.jsonl" ] || continue
  "$ROOT/system/run" "$dir" "$OUT/$name.jsonl"
  python3 "$ROOT/tools/validate.py" "$dir" "$OUT/$name.jsonl" | tail -n 1
  "$ROOT/system/run" "$dir" "$OUT/.$name.repeat.jsonl" 2>/dev/null
  if cmp -s "$OUT/$name.jsonl" "$OUT/.$name.repeat.jsonl"; then echo "  reproducible: yes"; else echo "  reproducible: NO"; fi
  rm -f "$OUT/.$name.repeat.jsonl"
done
