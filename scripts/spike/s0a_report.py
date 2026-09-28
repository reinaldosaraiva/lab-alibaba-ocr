"""Spike S0-A (P001-S005): métricas Q4 do adapter vs base 8B vs piso det.

Lê as predições do s0a_batch (pred-{base,adapter}-{unseen,mrs50}.jsonl no
--out-dir), o gold (--review-home: data/eval/gold.all.jsonl +
results/gold.unseen.jsonl) e o piso determinístico (--floor), grava metrics.json
e imprime o bloco markdown no stdout (o report.md da sessão é montado a partir
dele).

Semântica (contrato §17 Q4 — convenção de REVIEW_HOME/scripts/score_review.py,
record-level no alinhamento gold∩pred): FP = registro clean (gold sem findings)
com >=1 finding na pred; recall = registro defect com >=1 finding na pred;
recall por eixo = eixos do gold presentes nos eixos da pred (por registro);
CWE exato = pares gold×pred pareados POR EIXO com cwe igual. Threshold
P007-S004 desligado: label.findings direto (verdict ignorado).

Veredito: `Q4: PASS` sse o recall de finding do adapter no unseen >= melhor
recall determinístico em >=1 eixo reportado (eixo do floor = grupo de
linguagem) E FP do adapter no unseen <= 10%; senão `Q4: FAIL`. A seção mrs50
(densidade de findings por hunk de MR merged aceite) é FP-proxy informativo,
FORA do critério.

Uso (o Makefile injeta REVIEW_HOME):
    make s0a-report
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmark"))
from run_floor import _wilson as wilson95
from s0a_batch import read_jsonl, unseen_ids

DEFAULT_OUT_DIR = Path("results/p001-s005-s0a")
DEFAULT_FLOOR = Path("results/p001-s003-benchmark/floor.json")
FP_GATE = 0.10
VARIANTS = ("base", "adapter")
INPUT_NAMES = ("unseen", "mrs50")
# eixo Spec (conformance) fica fora: não é avaliável a nível de hunk
AXES = (
    "correctness",
    "security",
    "regression",
    "failure_handling",
    "performance",
    "tests",
    "maintainability",
)
GROUPS = ("py", "c", "tsjs", "go")
LANGUAGE_TO_GROUP = {
    "python": "py",
    "c": "c",
    "typescript": "tsjs",
    "javascript": "tsjs",
    "go": "go",
}


def language_group(language: str) -> str | None:
    """Grupo do floor; None = fora das tabelas por linguagem (ex.: shell/rust)."""
    return LANGUAGE_TO_GROUP.get(str(language))


def _findings(record: dict[str, Any]) -> list[dict[str, Any]]:
    label = record.get("label")
    findings = label.get("findings") if isinstance(label, dict) else None
    if not isinstance(findings, list):
        return []
    return [item for item in findings if isinstance(item, dict)]


def has_findings(record: dict[str, Any]) -> bool:
    return bool(_findings(record))


def partition(records: dict[str, dict[str, Any]]) -> tuple[list[str], list[str]]:
    """(clean, defect) pelo GOLD — label.findings vazio conta como clean."""
    clean = [rid for rid, rec in records.items() if not has_findings(rec)]
    defect = [rid for rid, rec in records.items() if has_findings(rec)]
    return clean, defect


def fp_rate(
    gold: dict[str, dict[str, Any]], pred: dict[str, dict[str, Any]]
) -> tuple[float | None, int, int]:
    """(rate, fp, n_clean): registro clean com >=1 finding na pred."""
    clean, _ = partition(gold)
    ids = [rid for rid in clean if rid in pred]
    fp = sum(1 for rid in ids if has_findings(pred[rid]))
    rate = fp / len(ids) if ids else None
    return rate, fp, len(ids)


def recall(
    gold: dict[str, dict[str, Any]], pred: dict[str, dict[str, Any]]
) -> tuple[float | None, int, int]:
    """(rate, detected, n_defect): registro defect com >=1 finding na pred."""
    _, defect = partition(gold)
    ids = [rid for rid in defect if rid in pred]
    detected = sum(1 for rid in ids if has_findings(pred[rid]))
    rate = detected / len(ids) if ids else None
    return rate, detected, len(ids)


def recall_by_axis(
    gold: dict[str, dict[str, Any]], pred: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Por registro defect: eixo do gold presente nos eixos da pred.

    O eixo conta UMA vez por registro (conjunto), mesmo com vários findings
    no mesmo eixo; registros defect sem predição não entram no denominador.
    """
    axis_gold: dict[str, int] = {}
    axis_hit: dict[str, int] = {}
    _, defect = partition(gold)
    for rid in defect:
        if rid not in pred:
            continue
        g_axes = {f.get("axis") for f in _findings(gold[rid])}
        p_axes = {f.get("axis") for f in _findings(pred[rid])}
        for axis in g_axes:
            axis_gold[axis] = axis_gold.get(axis, 0) + 1
            if axis in p_axes:
                axis_hit[axis] = axis_hit.get(axis, 0) + 1
    return {
        axis: {
            "gold": n,
            "hit": axis_hit.get(axis, 0),
            "recall": axis_hit.get(axis, 0) / n if n else None,
        }
        for axis, n in sorted(axis_gold.items())
    }


def cwe_exact(
    gold: dict[str, dict[str, Any]], pred: dict[str, dict[str, Any]]
) -> tuple[float | None, int, int]:
    """(rate, exact, pairs): pareamento POR EIXO; o par exige cwe no gold."""
    _, defect = partition(gold)
    pairs = exact = 0
    for rid in defect:
        if rid not in pred:
            continue
        g_by_axis = {f.get("axis"): f for f in _findings(gold[rid])}
        p_by_axis = {f.get("axis"): f for f in _findings(pred[rid])}
        for axis, gf in g_by_axis.items():
            if not gf.get("cwe"):
                continue
            pf = p_by_axis.get(axis)
            if pf is None:
                continue
            pairs += 1
            if pf.get("cwe") == gf.get("cwe"):
                exact += 1
    rate = exact / pairs if pairs else None
    return rate, exact, pairs


def score(
    gold: dict[str, dict[str, Any]], pred: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Pacote record-level (semântica score_review) + Wilson 95%."""
    rate_fp, n_fp, n_clean = fp_rate(gold, pred)
    rate_rec, n_det, n_defect = recall(gold, pred)
    rate_cwe, n_exact, n_pairs = cwe_exact(gold, pred)
    ids = sorted(set(gold) & set(pred))
    valid = sum(1 for rid in ids if (pred[rid].get("strict_json") or {}).get("valid"))
    return {
        "n": len(ids),
        "missing": len(gold) - len(ids),
        "n_clean": n_clean,
        "n_defect": n_defect,
        "fp": n_fp,
        "fp_rate": rate_fp,
        "fp_wilson95": wilson95(rate_fp, n_clean),
        "detected": n_det,
        "recall": rate_rec,
        "recall_wilson95": wilson95(rate_rec, n_defect),
        "cwe_pairs": n_pairs,
        "cwe_exact": n_exact,
        "cwe_rate": rate_cwe,
        "cwe_wilson95": wilson95(rate_cwe, n_pairs),
        "strict_json_valid_rate": valid / len(ids) if ids else None,
        "recall_by_axis": recall_by_axis(gold, pred),
    }


def by_language(
    gold: dict[str, dict[str, Any]], pred: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """score por grupo do floor; linguagens sem grupo ficam só no agregado."""
    out: dict[str, dict[str, Any]] = {}
    for group in GROUPS:
        sub_gold = {
            rid: rec
            for rid, rec in gold.items()
            if language_group(str(rec.get("language", ""))) == group
        }
        if sub_gold:
            out[group] = score(sub_gold, pred)
    return out


def best_det_recall(floor: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Melhor recall determinístico por grupo entre ferramentas × slices.

    Conservador: máximo entre as slices full e unseen do floor.json — o
    adapter precisa bater o piso mais alto já reportado naquela linguagem.
    Grupos sem recall reportado (null/n-a) ficam de fora (eixo não reportado).
    """
    best: dict[str, dict[str, Any]] = {}
    slices = floor.get("slices") if isinstance(floor.get("slices"), dict) else {}
    for group in GROUPS:
        top: float | None = None
        source = ""
        for slice_name in ("full", "unseen"):
            tools = slices.get(slice_name)
            if not isinstance(tools, dict):
                continue
            for tool, cells in sorted(tools.items()):
                cell = cells.get(group) if isinstance(cells, dict) else None
                if not isinstance(cell, dict) or cell.get("status") != "ok":
                    continue
                value = cell.get("recall")
                if isinstance(value, (int, float)) and (top is None or value > top):
                    top = float(value)
                    source = f"{tool}/{slice_name}"
        if top is not None:
            best[group] = {"recall": top, "source": source}
    return best


def q4_verdict(adapter_unseen: dict[str, Any], floor: dict[str, Any]) -> dict[str, Any]:
    """PASS sse recall adapter >= melhor det em >=1 eixo E FP <= 10%."""
    best = best_det_recall(floor)
    by_group = adapter_unseen.get("by_group", {})
    axes: list[dict[str, Any]] = []
    for group in GROUPS:
        entry = best.get(group)
        if entry is None:
            continue
        adapter_value = by_group.get(group, {}).get("recall")
        axis_result = {
            "group": group,
            "det_recall": entry["recall"],
            "det_source": entry["source"],
            "adapter_recall": adapter_value,
            "beats": adapter_value is not None and adapter_value >= entry["recall"],
        }
        axes.append(axis_result)
    fp = adapter_unseen.get("fp_rate")
    fp_ok = fp is not None and fp <= FP_GATE
    recall_ok = any(axis["beats"] for axis in axes)
    return {
        "verdict": "PASS" if (recall_ok and fp_ok) else "FAIL",
        "fp_gate": FP_GATE,
        "adapter_fp_rate": fp,
        "fp_ok": fp_ok,
        "recall_ok": recall_ok,
        "axes": axes,
    }


def mrs50_summary(pred: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Densidade de findings por hunk (FP-proxy; sem gold nos MRs merged)."""
    n = len(pred)
    findings = sum(len(_findings(rec)) for rec in pred.values())
    with_f = sum(1 for rec in pred.values() if _findings(rec))
    valid = sum(
        1 for rec in pred.values() if (rec.get("strict_json") or {}).get("valid")
    )
    return {
        "n_hunks": n,
        "findings": findings,
        "findings_per_hunk": findings / n if n else None,
        "hunks_with_findings": with_f,
        "hunks_with_findings_rate": with_f / n if n else None,
        "strict_json_valid_rate": valid / n if n else None,
    }


def fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


def fmt_ratio(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def fmt_wilson(interval: list[float] | None) -> str:
    if not interval:
        return "n/a"
    return f"[{100 * interval[0]:.1f}%, {100 * interval[1]:.1f}%]"


def render_unseen(per_variant: dict[str, dict[str, Any]]) -> list[str]:
    lines = [
        "## S0-A — gold unseen (critério Q4)",
        "",
        "Métricas record-level (semântica score_review) no alinhamento",
        "gold∩pred; threshold desligado (label.findings direto).",
        "",
        "### Agregado",
        "",
        (
            "| variante | n (clean/defect) | FP | FP rate [Wilson95] |"
            " recall [Wilson95] | CWE exato [Wilson95] | strict JSON |"
        ),
        "|---|---|---|---|---|---|---|",
    ]
    for variant in VARIANTS:
        m = per_variant[variant]
        lines.append(
            f"| {variant} | {m['n']} ({m['n_clean']}/{m['n_defect']}) | {m['fp']} "
            f"| {fmt_pct(m['fp_rate'])} {fmt_wilson(m['fp_wilson95'])} "
            f"| {fmt_pct(m['recall'])} {fmt_wilson(m['recall_wilson95'])} "
            f"| {fmt_pct(m['cwe_rate'])} {fmt_wilson(m['cwe_wilson95'])} "
            f"(n={m['cwe_pairs']}) | {fmt_pct(m['strict_json_valid_rate'])} |"
        )
    missing = {v: per_variant[v]["missing"] for v in VARIANTS}
    if any(missing.values()):
        detail = ", ".join(f"{v}={n}" for v, n in missing.items())
        lines.append("")
        lines.append(f"!! registros unseen sem predição: {detail}")
    for variant in VARIANTS:
        lines.append("")
        lines.append(f"### Por linguagem — {variant}")
        lines.append("")
        lines.append(
            "| grupo | n (clean/defect) | FP rate | recall | CWE exato | strict JSON |"
        )
        lines.append("|---|---|---|---|---|---|")
        for group, m in per_variant[variant]["by_group"].items():
            lines.append(
                f"| {group} | {m['n']} ({m['n_clean']}/{m['n_defect']}) "
                f"| {fmt_pct(m['fp_rate'])} | {fmt_pct(m['recall'])} "
                f"| {fmt_pct(m['cwe_rate'])} (n={m['cwe_pairs']}) "
                f"| {fmt_pct(m['strict_json_valid_rate'])} |"
            )
        lines.append(
            "- linguagens sem grupo no floor (ex.: shell/rust) ficam fora da"
            " tabela, mas dentro do agregado"
        )
    lines.append("")
    lines.append("### Recall por eixo (agregado; eixo conta 1x por registro)")
    lines.append("")
    lines.append("| eixo | base | adapter |")
    lines.append("|---|---|---|")
    seen: set[str] = set()
    for variant in VARIANTS:
        seen.update(per_variant[variant]["recall_by_axis"])
    ordered = [a for a in AXES if a in seen] + sorted(seen - set(AXES))
    for axis in ordered:
        cells = []
        for variant in VARIANTS:
            entry = per_variant[variant]["recall_by_axis"].get(axis)
            cells.append(
                "n/a"
                if entry is None
                else f"{fmt_pct(entry['recall'])} ({entry['hit']}/{entry['gold']})"
            )
        lines.append(f"| {axis} | {cells[0]} | {cells[1]} |")
    return lines


def render_mrs50(per_variant: dict[str, dict[str, Any]]) -> list[str]:
    lines = [
        "## S0-A — mrs50 (hunks de MRs merged aceites)",
        "",
        "FP-proxy: MR merged = código aceito por humanos; densidade alta =",
        "barulho. FORA do critério Q4 — seção informativa.",
        "",
        (
            "| variante | hunks | findings | findings/hunk | hunks c/ finding |"
            " strict JSON |"
        ),
        "|---|---|---|---|---|---|",
    ]
    for variant in VARIANTS:
        m = per_variant[variant]
        lines.append(
            f"| {variant} | {m['n_hunks']} | {m['findings']} "
            f"| {fmt_ratio(m['findings_per_hunk'])} "
            f"| {fmt_pct(m['hunks_with_findings_rate'])} "
            f"| {fmt_pct(m['strict_json_valid_rate'])} |"
        )
    return lines


def render_q4(verdict: dict[str, Any], adapter: dict[str, Any]) -> list[str]:
    by_group = adapter.get("by_group", {})
    lines = [
        "## Piso determinístico e critério Q4 (§17)",
        "",
        "Eixo do critério = grupo de linguagem (granularidade do floor.json).",
        "Melhor recall det = máximo entre ferramentas × slices (full, unseen)",
        "— conservador: o adapter precisa bater o piso mais alto reportado.",
        "",
        (
            "| grupo | melhor recall det (fonte) | recall adapter (unseen) |"
            " adapter >= piso? |"
        ),
        "|---|---|---|---|",
    ]
    for axis in verdict["axes"]:
        group = axis["group"]
        det_cell = f"{axis['det_recall']:.6f} ({axis['det_source']})"
        if axis["adapter_recall"] is None:
            adapter_cell = "n/a"
        else:
            m = by_group.get(group, {})
            adapter_cell = (
                f"{m.get('detected')}/{m.get('n_defect')}"
                f" = {axis['adapter_recall']:.4f}"
            )
        verdict_cell = "sim" if axis["beats"] else "não"
        lines.append(f"| {group} | {det_cell} | {adapter_cell} | {verdict_cell} |")
    lines.append("")
    if verdict["adapter_fp_rate"] is None:
        lines.append("- FP gate: fp_rate n/a (sem registro clean no unseen)")
    else:
        mark = "<=" if verdict["fp_ok"] else ">"
        state = "ok" if verdict["fp_ok"] else "VIOLA"
        lines.append(
            f"- FP gate: fp_rate = {adapter['fp']}/{adapter['n_clean']}"
            f" = {verdict['adapter_fp_rate']:.4f} {mark} {FP_GATE:.2f}"
            f" -> {state}"
        )
    beats = sum(1 for axis in verdict["axes"] if axis["beats"])
    lines.append(
        f"- recall gate: adapter >= piso em {beats}/{len(verdict['axes'])}"
        " eixo(s) reportado(s)"
    )
    lines.append("")
    lines.append(f"Q4: {verdict['verdict']}")
    return lines


def load_pred(path: Path) -> dict[str, dict[str, Any]]:
    preds: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as fh:
        for number, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError as exc:
                raise SystemExit(f"{path}:{number} não é JSON: {exc}") from exc
            if not isinstance(rec, dict) or rec.get("id") is None:
                raise SystemExit(f"{path}:{number} sem id")
            preds[str(rec["id"])] = rec
    return preds


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-home", required=True, type=Path)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--floor", type=Path, default=DEFAULT_FLOOR)
    args = parser.parse_args()

    review_home = args.review_home.expanduser().resolve()
    if not review_home.is_dir():
        raise SystemExit(f"--review-home inválido: {review_home}")
    pred_paths = {
        f"{variant}-{name}": args.out_dir / f"pred-{variant}-{name}.jsonl"
        for variant in VARIANTS
        for name in INPUT_NAMES
    }
    missing = sorted(str(p) for p in pred_paths.values() if not p.exists())
    if missing:
        raise SystemExit(
            "predições ausentes (rode a matriz: make s0a-batch V=... I=...):\n  "
            + "\n  ".join(missing)
        )
    if not args.floor.exists():
        raise SystemExit(f"floor ausente: {args.floor}")

    gold_path = review_home / "data" / "eval" / "gold.all.jsonl"
    gold_all = {str(rec["id"]): rec for rec in read_jsonl(gold_path)}
    unseen = unseen_ids(review_home / "results" / "gold.unseen.jsonl")
    absent = [rid for rid in unseen if rid not in gold_all]
    if absent:
        raise SystemExit(
            f"{len(absent)} ids do unseen ausentes no gold.all.jsonl "
            f"(ex.: {absent[:3]})"
        )
    gold_unseen = {rid: gold_all[rid] for rid in unseen}

    preds = {key: load_pred(path) for key, path in pred_paths.items()}
    unseen_metrics = {
        variant: {
            **score(gold_unseen, preds[f"{variant}-unseen"]),
            "by_group": by_language(gold_unseen, preds[f"{variant}-unseen"]),
        }
        for variant in VARIANTS
    }
    mrs50_metrics = {
        variant: mrs50_summary(preds[f"{variant}-mrs50"]) for variant in VARIANTS
    }
    floor = json.loads(args.floor.read_text(encoding="utf-8"))
    verdict = q4_verdict(unseen_metrics["adapter"], floor)

    metrics = {
        "session": "P001-S005",
        "generated_at": _now_iso(),
        "fp_gate": FP_GATE,
        "sources": {
            "review_home": str(review_home),
            "gold": {"path": str(gold_path), "sha256": _sha256(gold_path)},
            "unseen_ids": len(unseen),
            "floor": str(args.floor),
            "preds": {key: str(path) for key, path in pred_paths.items()},
        },
        "unseen": unseen_metrics,
        "mrs50": mrs50_metrics,
        "floor_best_det_recall": best_det_recall(floor),
        "q4": verdict,
    }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = args.out_dir / "metrics.json"
    metrics_path.write_text(
        json.dumps(metrics, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    lines = (
        render_unseen(unseen_metrics)
        + [""]
        + render_mrs50(mrs50_metrics)
        + [""]
        + render_q4(verdict, unseen_metrics["adapter"])
    )
    print("\n".join(lines))
    print(f"\ns0a_report: {metrics_path} atualizado")
    return 0


if __name__ == "__main__":
    sys.exit(main())
