"""CLI: read one institution folder, write one JSON line per case."""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from triage.engine import run_folder  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: system/run <institution_folder> <output.jsonl>", file=sys.stderr)
        return 2
    folder, out_path = os.path.abspath(argv[1]), os.path.abspath(argv[2])
    if not os.path.isdir(folder):
        print(f"not a folder: {folder}", file=sys.stderr)
        return 2
    rows = run_folder(folder)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=False) + "\n")
    os.replace(tmp, out_path)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    print(f"{len(rows)} cases -> {out_path}  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
