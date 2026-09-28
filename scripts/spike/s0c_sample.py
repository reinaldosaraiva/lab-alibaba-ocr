"""Spike S0-C (P001-S004): amostra 100 findings para a precisão humana (Q6).

Método (frozen):
- Pool = todos os comments de todos os runs com status complete
  (results/p001-s004-s0c/raw/*.json).
- Se pool >= 100: amostra de 100 estratificada por modelo (50 por modelo,
  proporcional ao disponível se um modelo tiver < 50), sorteio determinístico
  com seed=42.
- Se pool < 100: usa o pool inteiro (registrado no report).
- Cada item traz contexto para o julgamento humano: repo, PR, arquivo,
  linhas, existing_code, conteúdo do comment, modelo, effort.
- Saída: results/p001-s004-s0c/sampling.jsonl + templates de anotação
  (annotations-r1.jsonl / annotations-r2.jsonl) para os 2 revisores.

Julgamento (2 revisores humanos, independente):
    "true"  = finding válido (defeito/risco real no diff)
    "false" = falso positivo, inválido ou sem relação com o diff
Kappa de Cohen binário computado no report.

Uso:
    python3 scripts/spike/s0c_sample.py
"""

from __future__ import annotations

import json
import random
from pathlib import Path

RAW = Path("results/p001-s004-s0c/raw")
OUT = Path("results/p001-s004-s0c")
SEED = 42
N_TARGET = 100


def pool() -> list[dict]:
    items: list[dict] = []
    for f in sorted(RAW.glob("*.json")):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if doc.get("status") not in ("complete", "success"):
            continue
        llm = doc.get("llm") or {}
        model = llm.get("model", "?")
        for i, c in enumerate(doc.get("comments") or []):
            items.append(
                {
                    "run": f.name,
                    "model": model,
                    "index": i,
                    "path": c.get("path"),
                    "start_line": c.get("start_line"),
                    "end_line": c.get("end_line"),
                    "anchored": (c.get("start_line") or 0) > 0,
                    "category": c.get("category"),
                    "severity": c.get("severity"),
                    "existing_code": c.get("existing_code", "")[:2000],
                    "content": c.get("content", "")[:4000],
                }
            )
    return items


def stratified(items: list[dict], n: int, seed: int) -> list[dict]:
    by_model: dict[str, list[dict]] = {}
    for it in items:
        by_model.setdefault(it["model"], []).append(it)
    models = sorted(by_model)
    if len(models) <= 1 or n >= len(items):
        rng = random.Random(seed)
        return rng.sample(items, min(n, len(items)))
    per = n // len(models)
    extra = n - per * len(models)
    rng = random.Random(seed)
    out: list[dict] = []
    for j, m in enumerate(models):
        k = per + (1 if j < extra else 0)
        pool_m = by_model[m]
        out.extend(rng.sample(pool_m, min(k, len(pool_m))))
    return out


def main() -> None:
    items = pool()
    print(f"s0c_sample: pool com {len(items)} findings")
    if not items:
        raise SystemExit("pool vazio — rode o batch antes")
    sample = stratified(items, N_TARGET, SEED)
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "sampling.jsonl", "w", encoding="utf-8") as fh:
        for i, it in enumerate(sample, 1):
            row = {"sample_id": f"S{i:03d}", **it}
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    for reviewer in ("r1", "r2"):
        ann_path = OUT / f"annotations-{reviewer}.jsonl"
        if ann_path.exists():
            with open(ann_path, encoding="utf-8") as fh:
                filled = [json.loads(line).get("verdict") for line in fh]
            if any(filled):
                raise SystemExit(
                    f"{ann_path} já contém anotações — recusando sobrescrever"
                )
        with open(ann_path, "w", encoding="utf-8") as fh:
            for i in range(1, len(sample) + 1):
                fh.write(
                    json.dumps(
                        {
                            "sample_id": f"S{i:03d}",
                            "verdict": "",
                            "note": "",
                        }
                    )
                    + "\n"
                )
    n_anchored = sum(1 for it in sample if it["anchored"])
    print(
        f"s0c_sample: sampling.jsonl com {len(sample)} findings "
        f"({n_anchored} ancorados) + templates de anotação"
    )


if __name__ == "__main__":
    raise SystemExit(main())
