"""Spike S0-A (P001-S005): inferência local do adapter `review` vs base 8B.

Matriz do contrato congelado (§17 Q4, plans/P001-design-contract.md):
    variantes = base    -> REVIEW_HOME/models/qwen3-8b-4bit SEM adapter (controle)
              = adapter -> mesmo modelo + REVIEW_HOME/adapters/review (LoRA)
    entradas  = unseen -> registros de gold.all.jsonl cujo id está em
                          results/gold.unseen.jsonl (255)
              = mrs50  -> hunks (new_len <= 200 linhas) do diff base..head dos
                          50 MRs de data/mrs50/index.jsonl

Convenções de inferência copiadas de REVIEW_HOME/scripts/eval_review_adapters.py:
system prompt canônico (1a linha do train_v2), template Qwen3 com
enable_thinking=False, prefix cache do system prompt com trim por registro, e
validação strict-JSON + parse/enriquecimento IMPORTADOS de lá (o import traz
label_hunks no mesmo diretório; nada reimplementado). Decodificação GREEDY
explícita via make_sampler(temp=0.0): a mlx_lm instalada expõe `sampler=` em
generate_step, não `temp=`.

Os clones dos MRs (gitignored, .qwen/tmp/mrs50/) são produzidos por
scripts/spike/mrs50_clone.py e apenas LIDOS aqui.

Resumível: ids já presentes no arquivo de saída são pulados (append).
Saída (mesmo shape do eval_review_adapters):
    <out-dir>/pred-<variant>-<inputs>.jsonl

Uso (o Makefile injeta REVIEW_HOME e o .venv-train):
    make s0a-batch V=adapter I=unseen
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

DEFAULT_OUT_DIR = Path("results/p001-s005-s0a")
MRS50_INDEX = Path("data/mrs50/index.jsonl")
MRS50_CLONES = Path(".qwen/tmp/mrs50")
MAX_HUNK_LINES = 200
GIT_TIMEOUT_SEC = 1800
DIFF_PATHSPECS = ("*.py", "*.go", "*.ts", "*.js", "*.tsx", "*.jsx")
# index.language -> nome de linguagem usado no prompt (o gold usa nomes cheios)
LANG_BY_INDEX = {"python": "python", "go": "go", "tsjs": "typescript"}
HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

logger = logging.getLogger("s0a_batch")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Linhas não vazias de um JSONL; objeto por linha ou falha clara."""
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for number, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except ValueError as exc:
                raise SystemExit(f"{path}:{number} não é JSON: {exc}") from exc
            if not isinstance(payload, dict):
                raise SystemExit(f"{path}:{number} não é objeto")
            records.append(payload)
    return records


def unseen_ids(path: Path) -> list[str]:
    """Ids do conjunto unseen; aceita registro completo ou linha com id puro."""
    ids: list[str] = []
    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                payload: object = json.loads(line)
            except ValueError:
                payload = line
            if isinstance(payload, dict) and payload.get("id"):
                ids.append(str(payload["id"]))
            elif isinstance(payload, str) and payload:
                ids.append(payload)
    return list(dict.fromkeys(ids))


def load_unseen(review_home: Path) -> list[dict[str, Any]]:
    """Registros do gold (ordem do arquivo) cujo id está no conjunto unseen."""
    wanted = set(unseen_ids(review_home / "results" / "gold.unseen.jsonl"))
    records = [
        rec
        for rec in read_jsonl(review_home / "data" / "eval" / "gold.all.jsonl")
        if str(rec.get("id")) in wanted
    ]
    missing = wanted - {str(rec["id"]) for rec in records}
    if missing:
        raise SystemExit(
            f"{len(missing)} ids do unseen ausentes no gold.all.jsonl "
            f"(ex.: {sorted(missing)[:3]})"
        )
    logger.info("unseen: %d ids, %d registros", len(wanted), len(records))
    return records


def parse_hunks(diff_text: str) -> list[dict[str, Any]]:
    """Hunks de um diff unificado: [{file, new_start, new_len, diff}].

    O path vem do cabeçalho `+++ b/<path>`; arquivo deletado (+++ /dev/null)
    fica com file=None para o chamador descartar. Seção nova de arquivo só
    aparece após `diff --git`, então o hunk corrente termina ali — linhas de
    conteúdo sempre começam com ' ', '+', '-' ou '\\', nunca com os marcadores.
    """
    hunks: list[dict[str, Any]] = []
    file_name: str | None = None
    current: dict[str, Any] | None = None
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            current = None
            continue
        if current is None and line.startswith("+++ "):
            target = line[4:].strip()
            file_name = None if target == "/dev/null" else target.removeprefix("b/")
            continue
        if line.startswith("@@"):
            header = HUNK_HEADER.match(line)
            if header is None:
                continue
            current = {
                "file": file_name,
                "new_start": int(header.group(1)),
                "new_len": int(header.group(2)) if header.group(2) else 1,
                "diff": line,
            }
            hunks.append(current)
            continue
        if current is not None:
            current["diff"] += "\n" + line
    return hunks


def mr_diff(clone: Path, base: str, head: str) -> str:
    """Diff do MR restrito às extensões revisáveis (stderr capturado)."""
    command = [
        "git",
        "-C",
        str(clone),
        "diff",
        f"{base}..{head}",
        "--",
        *DIFF_PATHSPECS,
    ]
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SystemExit(
            f"git diff {base}..{head} em {clone}: timeout após {GIT_TIMEOUT_SEC}s"
        ) from exc
    if proc.returncode != 0:
        raise SystemExit(
            f"git diff {base}..{head} em {clone} saiu {proc.returncode}: "
            f"{proc.stderr[-1500:]}"
        )
    return proc.stdout


def load_mrs50(index_path: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Um registro por hunk (new_len <= 200) de cada MR do index."""
    counts = {
        "mrs": 0,
        "files": 0,
        "hunks": 0,
        "emitted": 0,
        "skipped_long": 0,
        "skipped_deleted": 0,
    }
    records: list[dict[str, Any]] = []
    files_seen: set[str] = set()
    rows = read_jsonl(index_path)
    counts["mrs"] = len(rows)
    for row in rows:
        clone = MRS50_CLONES / str(row["repo"]).replace("/", "__")
        if not (clone / ".git").exists():
            raise SystemExit(
                f"clone ausente: {clone} — rode scripts/spike/mrs50_clone.py antes"
            )
        language = LANG_BY_INDEX.get(str(row["language"]))
        if language is None:
            raise SystemExit(
                f"linguagem não mapeada no index: {row['language']!r} ({row['id']})"
            )
        subject = f"{row['repo']}#{row['pr']}"
        diff_text = mr_diff(clone, str(row["base"]), str(row["head"]))
        for hunk in parse_hunks(diff_text):
            counts["hunks"] += 1
            if hunk["file"] is None:
                counts["skipped_deleted"] += 1
                continue
            if hunk["new_len"] > MAX_HUNK_LINES:
                counts["skipped_long"] += 1
                continue
            files_seen.add(hunk["file"])
            counts["emitted"] += 1
            record = {
                "id": f"{row['id']}#{hunk['file']}:{hunk['new_start']}",
                "file": hunk["file"],
                "language": language,
                "subject": subject,
                "diff": hunk["diff"],
            }
            records.append(record)
    counts["files"] = len(files_seen)
    return records, counts


def done_ids(path: Path) -> set[str]:
    """Ids já presentes no arquivo de saída (resume append-only)."""
    if not path.exists():
        return set()
    ids: set[str] = set()
    with path.open(encoding="utf-8") as fh:
        for number, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except ValueError as exc:
                logger.warning(
                    "%s:%d ilegível, ignorada no resume: %s", path, number, exc
                )
                continue
            if isinstance(payload, dict) and payload.get("id") is not None:
                ids.add(str(payload["id"]))
    return ids


def run_inference(
    model: Any,
    tokenizer: Any,
    records: list[dict[str, Any]],
    out_path: Path,
    system_prompt: str,
    max_tokens: int,
    greedy: Any,
    check_strict_json: Callable[..., Any],
    parse_and_enrich_label: Callable[..., Any],
) -> int:
    """Gera e ANEXA uma predição por registro; retorna quantos processou.

    Prefix cache do system prompt com trim por registro — mesmo mecanismo do
    eval_review_adapters.evaluate_gold.
    """
    import mlx.core as mx
    from mlx_lm import generate
    from mlx_lm.models.cache import make_prompt_cache

    prefix_messages = [{"role": "system", "content": system_prompt}]
    prefix_str = tokenizer.apply_chat_template(
        prefix_messages, tokenize=False, add_generation_prompt=False
    )
    prefix_tokens = mx.array(tokenizer.encode(prefix_str))
    logger.info("prefill do system prompt (%d tokens)...", prefix_tokens.size)
    cache = make_prompt_cache(model)
    model(prefix_tokens[None], cache=cache)
    mx.eval([c.state for c in cache])
    base_offset = cache[0].offset
    prefix_len = len(prefix_str)

    total = len(records)
    logger.info("inferência: %d registros (greedy, thinking off)", total)
    start_time = time.monotonic()
    with out_path.open("a", encoding="utf-8") as fh:
        for idx, rec in enumerate(records, 1):
            user_prompt = (
                f"Arquivo: {rec.get('file', '')}\n"
                f"Linguagem: {rec.get('language', '')}\n"
                f"Commit: {rec.get('subject', '')}\n\n"
                f"```diff\n{rec['diff']}\n```"
            )
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
            full_prompt = tokenizer.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True,
                enable_thinking=False,
            )
            raw_out = generate(
                model,
                tokenizer,
                prompt=full_prompt[prefix_len:],
                prompt_cache=cache,
                max_tokens=max_tokens,
                sampler=greedy,
                verbose=False,
            )
            n_added = cache[0].offset - base_offset
            for c in cache:
                c.trim(n_added)

            strict_valid, strict_data, strict_err = check_strict_json(raw_out)
            label = parse_and_enrich_label(raw_out)
            fh.write(
                json.dumps(
                    {
                        "id": rec["id"],
                        "raw_text": raw_out,
                        "strict_json": {
                            "valid": strict_valid,
                            "error": strict_err,
                            "findings_count": (
                                len(strict_data.get("findings", []))
                                if strict_valid
                                else 0
                            ),
                        },
                        "label": label,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            fh.flush()
            if idx % 25 == 0 or idx == total:
                elapsed = time.monotonic() - start_time
                logger.info(
                    "  %d/%d %s (%.1f s, %.2f s/ex)",
                    idx,
                    total,
                    rec["id"],
                    elapsed,
                    elapsed / idx,
                )
    return total


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", required=True, choices=("base", "adapter"))
    parser.add_argument("--inputs", required=True, choices=("unseen", "mrs50"))
    parser.add_argument("--review-home", required=True, type=Path)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--max-tokens", type=int, default=1500)
    parser.add_argument(
        "--limit",
        type=int,
        help="processa no máximo N registros novos (após pular os já feitos)",
    )
    args = parser.parse_args()

    review_home = args.review_home.expanduser().resolve()
    if not review_home.is_dir():
        raise SystemExit(f"--review-home inválido: {review_home}")
    model_path = review_home / "models" / "qwen3-8b-4bit"
    adapter_path = review_home / "adapters" / "review"
    if not model_path.is_dir():
        raise SystemExit(f"modelo base ausente em {model_path}")
    if args.variant == "adapter" and not adapter_path.is_dir():
        raise SystemExit(f"adapter ausente em {adapter_path}")

    # parse/validação do output vêm do review-model; o import de
    # eval_review_adapters também resolve label_hunks (mesmo diretório)
    sys.path.insert(0, str(review_home / "scripts"))
    from eval_review_adapters import check_strict_json, parse_and_enrich_label

    with (review_home / "data" / "train_v2" / "train.jsonl").open(
        encoding="utf-8"
    ) as fh:
        system_prompt = json.loads(fh.readline())["messages"][0]["content"]

    if args.inputs == "unseen":
        records = load_unseen(review_home)
    else:
        records, counts = load_mrs50(MRS50_INDEX)
        logger.info(
            "mrs50: %s MRs, %s arquivos, %s hunks -> %s registros "
            "(pulados: %s > %d linhas, %s de arquivo deletado)",
            counts["mrs"],
            counts["files"],
            counts["hunks"],
            counts["emitted"],
            counts["skipped_long"],
            MAX_HUNK_LINES,
            counts["skipped_deleted"],
        )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out_dir / f"pred-{args.variant}-{args.inputs}.jsonl"
    done = done_ids(out_path)
    pending = [rec for rec in records if str(rec["id"]) not in done]
    if args.limit is not None:
        pending = pending[: args.limit]
    logger.info(
        "%s: %d registros carregados, %d já feitos, %d pendentes",
        out_path.name,
        len(records),
        len(done),
        len(pending),
    )
    if not pending:
        return 0

    # import lazy do MLX: erros de entrada falham antes de carregar o modelo
    from mlx_lm import load
    from mlx_lm.sample_utils import make_sampler

    if args.variant == "adapter":
        logger.info("carregando %s + adapter %s", model_path, adapter_path)
        model, tokenizer = load(str(model_path), adapter_path=str(adapter_path))
    else:
        logger.info("carregando %s (sem adapter — controle)", model_path)
        model, tokenizer = load(str(model_path))
    greedy = make_sampler(temp=0.0)

    written = run_inference(
        model,
        tokenizer,
        pending,
        out_path,
        system_prompt,
        args.max_tokens,
        greedy,
        check_strict_json,
        parse_and_enrich_label,
    )
    logger.info("%s: fim (%d novos registros)", out_path.name, written)
    return 0


if __name__ == "__main__":
    sys.exit(main())
