"""Status dos runs do spike S0-C: resume de runs.jsonl + raw JSONs.

Uso:
    python3 scripts/spike/s0c_status.py
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

RAW = Path("results/p001-s004-s0c/raw")
RUNS = Path("results/p001-s004-s0c/runs.jsonl")


def main() -> None:
    if RUNS.exists():
        with open(RUNS, encoding="utf-8") as fh:
            recs = [json.loads(line) for line in fh]
        c = Counter((r["status"], r["model"]) for r in recs)
        print("runs.jsonl por (status, modelo):")
        for k, v in sorted(c.items()):
            print(f"  {k}: {v}")
    print("\nraw JSONs:")
    for p in sorted(RAW.glob("*.json")):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"  {p.name}: JSON inválido")
            continue
        s = doc.get("summary") or {}
        print(
            f"  {p.name}: status={doc.get('status')} "
            f"files={s.get('files_reviewed')} comments={s.get('comments')} "
            f"tok={s.get('total_tokens')} "
            f"warnings={len(doc.get('warnings') or [])} "
            f"msg={str(doc.get('message', ''))[:70]}"
        )


if __name__ == "__main__":
    main()
