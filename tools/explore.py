"""Print the shape of an institution folder: files, row counts, field names
and example values. Run this first on the real data and add any field names
the loader does not know to the aliases in system/triage/data.py.

usage: python3 tools/explore.py <institution_folder>
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter


def main(folder: str) -> None:
    for name in sorted(os.listdir(folder)):
        p = os.path.join(folder, name)
        if os.path.isdir(p):
            files = sorted(os.listdir(p))
            prefixes = Counter(f.split("-")[0] for f in files)
            print(f"\n{name}/  {len(files)} files  prefixes={dict(prefixes.most_common(12))}")
            continue
        if not name.endswith(".jsonl"):
            print(f"\n{name}  ({os.path.getsize(p):,} bytes)")
            continue
        rows = [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]
        keys = Counter(k for r in rows for k in r)
        print(f"\n{name}  rows={len(rows)}")
        for k, c in keys.most_common():
            vals = Counter(json.dumps(r.get(k))[:60] for r in rows if k in r)
            ex = ", ".join(v for v, _ in vals.most_common(4))
            print(f"  {k:<32} {c:>6}  distinct={len(vals):<6} e.g. {ex}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    main(sys.argv[1])
