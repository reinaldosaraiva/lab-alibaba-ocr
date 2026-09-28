"""Spike S0-C (P001-S004): batch de 200 runs do bin/ocr sobre o mrs50.

Matriz pré-registrada (DR5, plans/P001-S004-results.md):
    50 MRs × 2 modelos × 2 efforts (medium, high) = 200 runs

Por run (endpoint via env OCR_LLM_* — o `ocr review` ignora
OCR_CONFIG_PATH por design; sem `--provider`, a resolução cai nas envs):
    OCR_LLM_URL=<url> OCR_LLM_TOKEN=<key> OCR_LLM_PROTOCOL=openai \
    bin/ocr review \
        --repo <clone> --from <base> --to pr-<N> \
        --format json --audience agent --effort <e> \
        --model <m> --max-tokens-budget 1000000 \
        --output <raw>/<id>__<model>__<effort>.json

Modelos (custo zero por assinatura — dono, 2026-09-23); url/key/protocolo
lidos de .lab/ocr-config.json (custom_providers, gitignored):
    qwen38-27b      -> magalu           (médio)
    deepseek-v4-pro -> bailian-tokenplan (frontier)

Saídas:
    results/p001-s004-s0c/raw/<id>__<model>__<effort>.json  (JSON bruto do OCR)
    results/p001-s004-s0c/runs.jsonl  (1 linha por run: métricas + status)

Resumível: run com raw JSON de status "success" já presente é pulado
(falha/timeout é reexecutado). Concorrência baixa (--concurrency, default 2)
para respeitar os limites dos planos de assinatura.

Uso:
    python3 scripts/spike/s0c_batch.py [--concurrency 2] [--only <substrings>]
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import subprocess
import sys
import time
from pathlib import Path

INDEX = Path("data/mrs50/index.jsonl")
OUT_DIR = Path("results/p001-s004-s0c")
RAW_DIR = OUT_DIR / "raw"
RUNS = OUT_DIR / "runs.jsonl"
WORKDIR = Path(".qwen/tmp/mrs50")
OCR = Path("bin/ocr")
CONFIG = Path(".lab/ocr-config.json")
MAX_TOKENS_BUDGET = 1_000_000
RUN_TIMEOUT_SEC = 30 * 60

MODELS = {
    "qwen38-27b": "magalu",
    "deepseek-v4-pro": "bailian-tokenplan",
}
EFFORTS = ("medium", "high")


def start_proxy() -> tuple[subprocess.Popen, str] | None:
    """Sobe o proxy local de terminação TLS para o gateway magalu.

    Go em darwin valida TLS contra o keychain do macOS (ignora
    SSL_CERT_FILE). O proxy em 127.0.0.1 usa a CA local para verificar
    o gateway. Retorna (Popen, url) ou None se nenhum modelo usa o magalu.
    """
    if not any(p == "magalu" for p in MODELS.values()):
        return None
    proc = subprocess.Popen(
        [sys.executable, "scripts/spike/local_llm_proxy.py"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    line = proc.stdout.readline().strip()
    # formato: local_llm_proxy: http://127.0.0.1:<port>/v1 -> https://...
    if not line.startswith("local_llm_proxy: http://"):
        proc.terminate()
        raise SystemExit(f"proxy não subiu: {line}")
    url = line.split(" -> ")[0].rsplit(" ", 1)[1]
    return proc, url


def load_endpoints(magalu_url: str | None) -> dict[str, dict]:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    out: dict[str, dict] = {}
    for model, provider in MODELS.items():
        entry = cfg["custom_providers"][provider]
        url = entry["url"]
        if provider == "magalu":
            if not magalu_url:
                raise SystemExit("proxy magalu não está no ar")
            url = magalu_url
        out[model] = {
            "url": url,
            "token": entry["api_key"],
            "protocol": entry.get("protocol", "openai"),
            "extra_env": {},
        }
    return out


def repo_dir(full_name: str) -> Path:
    return WORKDIR / full_name.replace("/", "__")


def raw_path(row: dict, model: str, effort: str) -> Path:
    safe_model = model.replace("/", "-")
    return RAW_DIR / f"{row['id']}__{safe_model}__{effort}.json"


def load_done() -> set[str]:
    """Runs em estado terminal: complete/success (ok) ou skipped (diff sem
    itens revisáveis — estado determinístico, reexecutar não muda nada).
    partial também conta como terminal: DR5 item 7 — run parcial não é
    repetido (resume não deve reexecutá-lo)."""
    done: set[str] = set()
    if not RAW_DIR.exists():
        return done
    for f in RAW_DIR.glob("*.json"):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if doc.get("status") in ("success", "complete", "skipped", "partial"):
            done.add(f.name)
    return done


def run_one(row: dict, model: str, effort: str, endpoints: dict) -> dict:
    raw = raw_path(row, model, effort)
    provider = MODELS[model]
    repo = repo_dir(row["repo"])
    ep = endpoints[model]
    env = dict(
        os.environ,
        OCR_LLM_URL=ep["url"],
        OCR_LLM_TOKEN=ep["token"],
        OCR_LLM_PROTOCOL=ep["protocol"],
        **ep["extra_env"],
    )
    cmd = [
        str(OCR),
        "review",
        "--repo",
        str(repo),
        "--from",
        row["base"],
        "--to",
        row["head"],
        "--format",
        "json",
        "--audience",
        "agent",
        "--effort",
        effort,
        "--model",
        model,
        "--max-tokens-budget",
        str(MAX_TOKENS_BUDGET),
        "--output",
        str(raw),
    ]
    t0 = time.monotonic()
    rec = {
        "id": row["id"],
        "repo": row["repo"],
        "pr": row["pr"],
        "language": row["language"],
        "model": model,
        "provider": provider,
        "effort": effort,
    }
    try:
        proc = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT_SEC,
            check=False,
        )
        rec["exit"] = proc.returncode
        if proc.returncode != 0:
            rec["status"] = "error"
            rec["stderr_tail"] = proc.stderr[-1500:]
    except subprocess.TimeoutExpired:
        rec["exit"] = None
        rec["status"] = "timeout"
    rec["wall_sec"] = round(time.monotonic() - t0, 1)

    if raw.exists():
        try:
            doc = json.loads(raw.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            doc = {}
        summary = doc.get("summary") or {}
        comments = doc.get("comments") or []
        anchored = sum(1 for c in comments if c.get("start_line", 0) > 0)
        rec.update(
            {
                "status": doc.get("status", rec.get("status", "parse_error")),
                "files_reviewed": summary.get("files_reviewed"),
                "input_tokens": summary.get("input_tokens"),
                "output_tokens": summary.get("output_tokens"),
                "total_tokens": summary.get("total_tokens"),
                "cache_read_tokens": summary.get("cache_read_tokens"),
                "cache_write_tokens": summary.get("cache_write_tokens"),
                "ocr_elapsed": summary.get("elapsed"),
                "budget_exceeded": summary.get("budget_exceeded"),
                "n_comments": len(comments),
                "n_anchored": anchored,
                "anchoring": round(anchored / len(comments), 4) if comments else None,
                "session_id": doc.get("session_id"),
            }
        )
    return rec


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="roda só runs cujo id|modelo|effort contenha TODOS os substrings (AND; repetível)",
    )
    args = parser.parse_args()

    proxy = start_proxy()
    magalu_url = proxy[1] if proxy else None
    with open(INDEX, encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh]
    endpoints = load_endpoints(magalu_url)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    done = load_done()

    tasks = []
    for row in rows:
        for model in MODELS:
            for effort in EFFORTS:
                name = raw_path(row, model, effort).name
                if name in done:
                    continue
                if args.only and not all(
                    s in f"{row['id']}|{model}|{effort}" for s in args.only
                ):
                    continue
                tasks.append((row, model, effort))

    print(f"s0c_batch: {len(tasks)} runs pendentes (concorrência {args.concurrency})")
    try:
        if not tasks:
            return
        with (
            RUNS.open("a", encoding="utf-8") as fh,
            cf.ThreadPoolExecutor(max_workers=args.concurrency) as pool,
        ):
            futures = {
                pool.submit(run_one, row, model, effort, endpoints): (
                    row,
                    model,
                    effort,
                )
                for row, model, effort in tasks
            }
            for i, fut in enumerate(cf.as_completed(futures), 1):
                rec = fut.result()
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
                print(
                    f"[{i}/{len(tasks)}] {rec['id']} {rec['model']} {rec['effort']}: "
                    f"{rec['status']} tok={rec.get('total_tokens')} "
                    f"wall={rec['wall_sec']}s comments={rec.get('n_comments')}",
                    flush=True,
                )
        print("s0c_batch: fim")
    finally:
        if proxy:
            proxy[0].terminate()
            proxy[0].wait(timeout=10)


if __name__ == "__main__":
    sys.exit(main())
