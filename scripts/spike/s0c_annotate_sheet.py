"""Spike S0-C (P001-S004): renderiza a folha de anotação para os revisores.

Lê results/p001-s004-s0c/sampling.jsonl e gera um markdown legível
(annotate-sheet.md) com um bloco por finding: contexto (repo/PR/arquivo/
linhas/existing_code) + conteúdo do comment + campo de veredito.

O revisor preenche o veredito em annotations-r1.jsonl / annotations-r2.jsonl
(true = finding válido; false = falso positivo/inválido). A folha é só o
auxílio de leitura; o artefato de anotação continua sendo o JSONL.

Uso:
    python3 scripts/spike/s0c_annotate_sheet.py
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path("results/p001-s004-s0c")
SAMP = OUT / "sampling.jsonl"


def main() -> None:
    with open(SAMP, encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh]
    lines: list[str] = []
    lines.append("# S0-C — Folha de anotação (precisão humana, Q6)")
    lines.append("")
    lines.append(
        f"- Findings na amostra: {len(rows)} (pool: qwen38-27b esgotou em 49; deepseek-v4-pro 50 de 117)"
    )
    lines.append(
        "- Veredito: `true` = finding válido (defeito/risco real no diff); `false` = falso positivo, inválido ou sem relação com o diff."
    )
    lines.append(
        "- Preencher em `annotations-r1.jsonl` (revisor 1) e `annotations-r2.jsonl` (revisor 2), campo `verdict` por `sample_id`."
    )
    lines.append("")
    for r in rows:
        lines.append(f"## {r['sample_id']} — {r['model']} (run {r['run']})")
        lines.append("")
        lines.append(
            f"- arquivo: `{r['path']}` linhas {r['start_line']}-{r['end_line']} (ancorado: {r['anchored']})"
        )
        lines.append(f"- categoria: {r['category']} | severidade: {r['severity']}")
        if r.get("existing_code"):
            lines.append("- código do diff:")
            lines.append("")
            lines.append("```")
            lines.append(r["existing_code"])
            lines.append("```")
        lines.append("")
        lines.append(f"> **Comment:** {r['content']}")
        lines.append("")
        lines.append("- [ ] `true`  -  [ ] `false`   nota: ____________________")
        lines.append("")
        lines.append("---")
        lines.append("")
    (OUT / "annotate-sheet.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"s0c_annotate_sheet: {OUT / 'annotate-sheet.md'} com {len(rows)} findings")


if __name__ == "__main__":
    main()
