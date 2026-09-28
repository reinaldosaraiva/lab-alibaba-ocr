"""Spike S0-C (P001-S004): valida as anotações e computa a precisão (Q6).

Valida annotations-r1.jsonl / annotations-r2.jsonl contra sampling.jsonl
(99/99, verdict true/false, ids idênticos) e computa:
- precisão global e por modelo (veredito r1, e consenso quando discordam);
- Cohen's kappa binário entre r1 e r2.

Uso:
    python3 scripts/spike/s0c_precision.py [--md]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

OUT = Path("results/p001-s004-s0c")
SAMP = OUT / "sampling.jsonl"


def load_annotations(rev: str) -> dict[str, str]:
    out: dict[str, str] = {}
    with open(OUT / f"annotations-{rev}.jsonl", encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            out[r["sample_id"]] = r["verdict"]
    return out


def validate(samp_ids: list[str], r1: dict[str, str], r2: dict[str, str]) -> list[str]:
    problems: list[str] = []
    for rev, ann in (("r1", r1), ("r2", r2)):
        missing = [i for i in samp_ids if i not in ann]
        extra = [i for i in ann if i not in samp_ids]
        bad = [i for i, v in ann.items() if v not in ("true", "false")]
        if missing:
            problems.append(f"{rev}: faltando {len(missing)}: {missing[:5]}")
        if extra:
            problems.append(f"{rev}: ids fora da amostra: {extra[:5]}")
        if bad:
            problems.append(f"{rev}: verdict inválido em {bad[:5]}")
    return problems


def cohen_kappa(a: dict[str, str], b: dict[str, str], ids: list[str]) -> float:
    """Kappa binário (true/false) sobre os ids comuns."""
    common = [i for i in ids if i in a and i in b]
    n = len(common)
    if n == 0:
        return float("nan")
    po = sum(1 for i in common if a[i] == b[i]) / n
    # marginais
    at = sum(1 for i in common if a[i] == "true") / n
    bt = sum(1 for i in common if b[i] == "true") / n
    pe = at * bt + (1 - at) * (1 - bt)
    if pe >= 1.0:
        return 1.0
    return (po - pe) / (1 - pe)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--md", action="store_true")
    args = parser.parse_args()

    with open(SAMP, encoding="utf-8") as fh:
        samp = [json.loads(line) for line in fh]
    ids = [r["sample_id"] for r in samp]
    r1 = load_annotations("r1")
    r2 = load_annotations("r2")
    problems = validate(ids, r1, r2)
    if problems:
        for p in problems:
            print("PROBLEMA:", p)
        raise SystemExit(1)

    kappa = cohen_kappa(r1, r2, ids)
    agree = sum(1 for i in ids if r1[i] == r2[i])

    def precision(ann: dict[str, str], filt: dict | None = None) -> tuple[int, int]:
        tp = fp = 0
        for r in samp:
            if filt and any(r.get(k) != v for k, v in filt.items()):
                continue
            if ann[r["sample_id"]] == "true":
                tp += 1
            else:
                fp += 1
        return tp, fp

    lines: list[str] = []
    lines.append("### Precisão humana (Q6)")
    lines.append("")
    lines.append(f"- Amostra: {len(ids)} findings (estratificada por modelo, seed=42).")
    lines.append(
        f"- Concordância r1/r2: {agree}/{len(ids)} ({agree / len(ids) * 100:.1f}%)"
    )
    lines.append(f"- **Cohen's kappa: {kappa:.3f}**")
    if agree == len(ids):
        lines.append(
            "- Nota: acordo perfeito torna as marginais idênticas (pe=1, kappa 0/0); "
            "kappa=1.000 por convenção — mede consistência intra-avaliador "
            "(2 passes do mesmo dono; ver annotation-log.md)."
        )
    lines.append("")
    lines.append(
        "| escopo | n | true (r1) | precisão (r1) | precisão (consenso r1∧r2) |"
    )
    lines.append(
        "|--------|---|-----------|---------------|----------------------------|"
    )
    scopes = [
        ("total", None),
        ("qwen38-27b", {"model": "qwen38-27b"}),
        ("deepseek-v4-pro", {"model": "deepseek-v4-pro"}),
    ]
    for label, filt in scopes:
        tp1, fp1 = precision(r1, filt)
        n = tp1 + fp1
        p1 = tp1 / n if n else 0
        # consenso: true só se os dois marcaram true
        tp_c = sum(
            1
            for r in samp
            if not (filt and any(r.get(k) != v for k, v in filt.items()))
            and r1[r["sample_id"]] == "true"
            and r2[r["sample_id"]] == "true"
        )
        p_c = tp_c / n if n else 0
        lines.append(f"| {label} | {n} | {tp1} | {p1 * 100:.1f}% | {p_c * 100:.1f}% |")
    lines.append("")
    disc = [i for i in ids if r1[i] != r2[i]]
    lines.append(
        f"- Discordâncias r1/r2: {len(disc)} ({', '.join(disc) if disc else 'nenhuma'})"
    )

    text = "\n".join(lines)
    if args.md:
        print(text)
    else:
        print(text)
        (OUT / "precision.md").write_text(text + "\n", encoding="utf-8")
        print(f"\ns0c_precision: {OUT / 'precision.md'} atualizado")


if __name__ == "__main__":
    main()
