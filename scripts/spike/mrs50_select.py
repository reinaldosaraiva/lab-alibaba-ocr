"""Spike S0-C (P001-S004): seleciona 50 MRs reais para o index.jsonl.

Método pré-registrado (DR5 — §17 Q2 d, Q6, Q8):

- 3 repos públicos com licença permissiva, um por linguagem (Q8):
  Python  -> psf/requests  (Apache-2.0)
  Go      -> gin-gonic/gin (MIT)
  TS/JS   -> axios/axios   (MIT)
- MRs = PRs MERGED com base na branch default do repo.
- Janela: merged_at em [2025-09-23, 2026-09-22] (12 meses até o kickoff).
- Tamanho: 20 <= additions+deletions <= 1500 e changed_files <= 25
  (fora disso: trivial demais ou mega-PR — ruído para o spike de custo/FPR).
- Sem drafts.
- Cota por linguagem: python 17, go 17, tsjs 16 (total 50).
- Determinístico: candidatos ordenados por (merged_at desc, number desc);
  toma os primeiros que passam nos filtros. Sem aleatoriedade.

Saída: data/mrs50/index.jsonl — só refs + metadados de seleção
(repo, pr, base, head, linguagem, licença, tamanhos). SEM payload de diff
(scope rule 6). O head ref local (pr-<N>) é criado pelo mrs50_clone.py.

Uso:
    python3 scripts/spike/mrs50_select.py [--out data/mrs50/index.jsonl]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime

API = "https://api.github.com"
USER_AGENT = "lab-alibaba-ocr-s0c/1.0 (P001-S004)"


def gh_token() -> str:
    out = subprocess.run(
        ["gh", "auth", "token"], capture_output=True, text=True, check=True
    ).stdout.strip()
    if not out:
        raise SystemExit("gh não autenticado — `gh auth login` primeiro")
    return out


# Candidatos por linguagem (Q8), em ordem de preferência; o primeiro repo que
# entregar a cota vence. Todos públicos com licença permissiva (Q2 d).
REPOS = {
    "python": [
        {"full_name": "psf/requests", "license": "Apache-2.0"},
        {"full_name": "pallets/flask", "license": "BSD-3-Clause"},
        {"full_name": "pytest-dev/pytest", "license": "MIT"},
    ],
    "go": [
        {"full_name": "gin-gonic/gin", "license": "MIT"},
        {"full_name": "prometheus/prometheus", "license": "Apache-2.0"},
        {"full_name": "gohugoio/hugo", "license": "Apache-2.0"},
    ],
    "tsjs": [
        {"full_name": "axios/axios", "license": "MIT"},
        {"full_name": "expressjs/express", "license": "MIT"},
        {"full_name": "vitejs/vite", "license": "MIT"},
    ],
}
QUOTAS = {"python": 17, "go": 17, "tsjs": 16}

WINDOW_START = "2025-09-23T00:00:00Z"
WINDOW_END = "2026-09-22T23:59:59Z"
MIN_LINES = 20
MAX_LINES = 1500
MAX_FILES = 25
MAX_PAGES_PER_REPO = 6  # 600 PRs fechados por repo — folga ampla
MAX_DETAIL_CHECKS = 150  # teto de fetches de detail por repo (rate limit)


_TOKEN: str | None = None


def api_get(url: str) -> list[dict]:
    global _TOKEN
    if _TOKEN is None:
        _TOKEN = gh_token()
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {_TOKEN}",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"GitHub API HTTP {exc.code} em {url}") from exc


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def in_window(merged_at: str) -> bool:
    ts = parse_ts(merged_at)
    return parse_ts(WINDOW_START) <= ts <= parse_ts(WINDOW_END)


def qualifies(pr: dict) -> bool:
    if pr.get("draft"):
        return False
    merged_at = pr.get("merged_at")
    if not merged_at or not in_window(merged_at):
        return False
    base = pr.get("base", {})
    if not base.get("ref"):
        return False
    total = (pr.get("additions") or 0) + (pr.get("deletions") or 0)
    if not (MIN_LINES <= total <= MAX_LINES):
        return False
    return (pr.get("changed_files") or 0) <= MAX_FILES


def _pr_one(full_name: str, number: int) -> dict:
    req_url = f"{API}/repos/{full_name}/pulls/{number}"
    global _TOKEN
    if _TOKEN is None:
        _TOKEN = gh_token()
    req = urllib.request.Request(
        req_url,
        headers={
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {_TOKEN}",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def candidate_list(full_name: str) -> list[dict]:
    """PRs merged na janela, mais recentes primeiro (tiebreak por número)."""
    url = f"{API}/repos/{full_name}/pulls?state=closed&per_page=100"
    out: list[dict] = []
    for page in range(1, MAX_PAGES_PER_REPO + 1):
        batch = api_get(f"{url}&page={page}")
        if not batch:
            break
        out.extend(p for p in batch if p.get("merged_at") and in_window(p["merged_at"]))
        if len(batch) < 100:
            break
    out.sort(key=lambda p: (parse_ts(p["merged_at"]), p["number"]), reverse=True)
    return out


def select_repo(lang: str, candidates: list[dict]) -> tuple[dict, list[dict]]:
    """Retorna (repo vencedor, MRs escolhidos). Tenta os candidatos em ordem."""
    quota = QUOTAS[lang]
    for spec in candidates:
        full_name = spec["full_name"]
        picked: list[dict] = []
        checked = 0
        for pr in candidate_list(full_name):
            if len(picked) >= quota:
                break
            if checked >= MAX_DETAIL_CHECKS:
                break
            if pr.get("draft") or not pr.get("base", {}).get("ref"):
                continue
            checked += 1
            detail = _pr_one(full_name, pr["number"])
            if qualifies(detail):
                picked.append(detail)
        if len(picked) >= quota:
            print(f"{full_name}: {len(picked)} MRs (verificados {checked})")
            return spec, picked
        print(f"{full_name}: só {len(picked)}/{quota} — tentando próximo candidato")
    raise SystemExit(f"{lang}: nenhum candidato entregou a cota {quota}")


def stats(lang: str, spec: dict) -> None:
    full_name = spec["full_name"]
    url = f"{API}/repos/{full_name}/pulls?state=closed&per_page=100"
    total = merged = in_win = 0
    for page in range(1, MAX_PAGES_PER_REPO + 1):
        batch = api_get(f"{url}&page={page}")
        if not batch:
            break
        total += len(batch)
        for pr in batch:
            if pr.get("merged_at"):
                merged += 1
                if in_window(pr["merged_at"]):
                    in_win += 1
        if len(batch) < 100:
            break
    print(
        f"{full_name}: {total} fechados / {merged} merged / "
        f"{in_win} na janela (tamanhos via detail)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="data/mrs50/index.jsonl")
    parser.add_argument("--stats", action="store_true", help="só diagnóstico")
    args = parser.parse_args()

    if args.stats:
        for lang in ("python", "go", "tsjs"):
            for spec in REPOS[lang]:
                stats(lang, spec)
        return

    rows: list[dict] = []
    for lang in ("python", "go", "tsjs"):
        spec, prs = select_repo(lang, REPOS[lang])
        for pr in prs:
            rows.append(
                {
                    "id": f"{spec['full_name'].replace('/', '--')}-{pr['number']}",
                    "repo": spec["full_name"],
                    "pr": pr["number"],
                    "base": pr["base"]["ref"],
                    "head": f"pr-{pr['number']}",
                    "language": lang,
                    "license": spec["license"],
                    "merged_at": pr["merged_at"],
                    "additions": pr.get("additions") or 0,
                    "deletions": pr.get("deletions") or 0,
                    "changed_files": pr.get("changed_files") or 0,
                }
            )

    if len(rows) != 50:
        raise SystemExit(f"esperava 50 MRs, obtive {len(rows)}")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.writelines(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    print(f"mrs50_select: {args.out} ({len(rows)} MRs)")
    for lang in ("python", "go", "tsjs"):
        n = sum(1 for r in rows if r["language"] == lang)
        print(f"  {lang}: {n}")


if __name__ == "__main__":
    sys.exit(main())
