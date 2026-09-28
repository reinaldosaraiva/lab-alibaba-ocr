"""Spike S0-C (P001-S004): gera as métricas do report a partir dos runs.

Lê results/p001-s004-s0c/runs.jsonl + raw/*.json e imprime em markdown as
seções quantitativas do report (tokens, custo, latência, ancoragem,
distribuição). A precisão humana (100 findings, 2 revisores, kappa) é
preenchida à parte após as anotações.

Uso:
    python3 scripts/spike/s0c_report.py [--md]   # --md imprime só o bloco md
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

OUT = Path("results/p001-s004-s0c")
RAW = OUT / "raw"
RUNS = OUT / "runs.jsonl"
MODELS = ("qwen38-27b", "deepseek-v4-pro")
EFFORTS = ("medium", "high")
MATRIX_TOTAL = 200


def load() -> list[dict]:
    """Um registro por célula da matriz (raw JSON é a fonte; runs.jsonl
    traz o wall do runner)."""
    wall: dict[str, dict] = {}
    if RUNS.exists():
        with open(RUNS, encoding="utf-8") as fh:
            for line in fh:
                r = json.loads(line)
                key = f"{r['id']}|{r['model']}|{r['effort']}"
                wall[key] = r  # última linha vence (reexecuções)
    cells: list[dict] = []
    for f in sorted(RAW.glob("*.json")):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        llm = doc.get("llm") or {}
        model = llm.get("model", "?")
        # effort/id vêm do nome do arquivo: <id>__<model>__<effort>.json
        base = f.name[: -len(".json")]
        rid, _, effort = base.rsplit("__", 2)
        s = doc.get("summary") or {}
        comments = doc.get("comments") or []
        anchored = sum(1 for c in comments if (c.get("start_line") or 0) > 0)
        rec = wall.get(f"{rid}|{model}|{effort}", {})
        cells.append(
            {
                "id": rid,
                "model": model,
                "effort": effort,
                "status": doc.get("status"),
                "input_tokens": s.get("input_tokens") or 0,
                "output_tokens": s.get("output_tokens") or 0,
                "total_tokens": s.get("total_tokens") or 0,
                "files_reviewed": s.get("files_reviewed") or 0,
                "n_comments": len(comments),
                "n_anchored": anchored,
                "budget_exceeded": bool(s.get("budget_exceeded")),
                "ocr_elapsed": s.get("elapsed"),
                "wall_sec": rec.get("wall_sec"),
            }
        )
    return cells


def pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def median(xs: list[float]) -> float:
    n = len(xs)
    if not n:
        return 0.0
    m = n // 2
    return xs[m] if n % 2 else (xs[m - 1] + xs[m]) / 2


def summarize(cells: list[dict]) -> list[str]:
    lines: list[str] = []
    by_status = Counter(c["status"] for c in cells)
    lines.append(f"- Células na matriz: {len(cells)}/{MATRIX_TOTAL}")
    lines.append(
        "- Status: " + ", ".join(f"{k} {v}" for k, v in sorted(by_status.items()))
    )

    ok = [c for c in cells if c["status"] == "complete"]
    skipped = [c for c in cells if c["status"] == "skipped"]
    lines.append(
        f"- Skipped (diff sem itens revisáveis): {len(skipped)} "
        f"({sorted({c['id'] for c in skipped})})"
    )

    lines.append("")
    lines.append("### Tokens e latência (runs complete)")
    lines.append("")
    lines.append(
        "| modelo | effort | n | tokens in (Σ) | tokens out (Σ) | tokens (Σ) | wall mediana (s) | wall média (s) | wall p95 (s) |"
    )
    lines.append(
        "|--------|--------|---|---------------|----------------|------------|------------------|---------------|--------------|"
    )
    for model in MODELS:
        for effort in EFFORTS:
            sub = [c for c in ok if c["model"] == model and c["effort"] == effort]
            if not sub:
                continue
            walls = sorted(c["wall_sec"] for c in sub if c["wall_sec"] is not None)
            med = median(walls)
            mean = sum(walls) / len(walls) if walls else 0
            p95 = walls[min(len(walls) - 1, int(0.95 * len(walls)))] if walls else 0
            lines.append(
                f"| {model} | {effort} | {len(sub)} "
                f"| {sum(c['input_tokens'] for c in sub):,} "
                f"| {sum(c['output_tokens'] for c in sub):,} "
                f"| {sum(c['total_tokens'] for c in sub):,} "
                f"| {med:.0f} | {mean:.0f} | {p95:.0f} |"
            )
    lines.append("")

    lines.append("### Findings e ancoragem (runs complete)")
    lines.append("")
    lines.append(
        "| modelo | effort | n runs | findings (Σ) | ancorados (Σ) | ancoragem | findings/run |"
    )
    lines.append(
        "|--------|--------|--------|--------------|---------------|-----------|--------------|"
    )
    for model in MODELS:
        for effort in EFFORTS:
            sub = [c for c in ok if c["model"] == model and c["effort"] == effort]
            if not sub:
                continue
            n_f = sum(c["n_comments"] for c in sub)
            n_a = sum(c["n_anchored"] for c in sub)
            anch = pct(n_a / n_f) if n_f else "n/a"
            lines.append(
                f"| {model} | {effort} | {len(sub)} | {n_f} | {n_a} | {anch} "
                f"| {n_f / len(sub):.2f} |"
            )
    lines.append("")

    lines.append("### Custo")
    lines.append("")
    lines.append(
        "- **$0.00 pay-as-you-go** — os dois endpoints são planos de assinatura "
        "(Magalu Coding Plan + Bailian Token Plan); o consumo é medido em tokens "
        "(colunas acima). Teto USD pré-registrado: $0.00 (DR5 item 2)."
    )
    n_be = sum(1 for c in ok if c["budget_exceeded"])
    lines.append(f"- Runs com `budget_exceeded` (1M tok/MR): {n_be}")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--md", action="store_true")
    args = parser.parse_args()
    cells = load()
    lines = summarize(cells)
    text = "\n".join(lines)
    if args.md:
        print(text)
    else:
        print(text)
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "metrics.md").write_text(text + "\n", encoding="utf-8")
        print(f"\ns0c_report: {OUT / 'metrics.md'} atualizado")


if __name__ == "__main__":
    main()
