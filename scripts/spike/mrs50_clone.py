"""Spike S0-C (P001-S004): clona os 3 repos do index e traz os refs dos PRs.

Para cada repo único em data/mrs50/index.jsonl:
  - git clone completo em .qwen/tmp/mrs50/<owner>__<name>/ (gitignored)
  - git fetch origin pull/<N>/head:pr-<N> para cada PR do index
  - valida que o base ref existe localmente

Idempotente: clone existente é reutilizado; fetch de ref já existente é
pulado. Workdir é transitório (reproduzível do index.jsonl — não é artifact).

Uso:
    python3 scripts/spike/mrs50_clone.py [--index data/mrs50/index.jsonl]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

WORKDIR = Path(".qwen/tmp/mrs50")


def run(args: list[str], cwd: str | None = None) -> str:
    proc = subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"{' '.join(args)} saiu {proc.returncode}:\n{proc.stderr[-2000:]}"
        )
    return proc.stdout


def ref_exists(ref: str, cwd: str) -> bool:
    """rev-parse --verify --quiet sai 1 quando o ref não existe (esperado)."""
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", ref],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return proc.returncode == 0


def repo_dir(full_name: str) -> Path:
    return WORKDIR / full_name.replace("/", "__")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", default="data/mrs50/index.jsonl")
    args = parser.parse_args()

    with open(args.index, encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh]
    repos: dict[str, list[dict]] = {}
    for row in rows:
        repos.setdefault(row["repo"], []).append(row)

    WORKDIR.mkdir(parents=True, exist_ok=True)
    for full_name, prs in repos.items():
        dest = repo_dir(full_name)
        if not (dest / ".git").exists():
            url = f"https://github.com/{full_name}.git"
            print(f"clonando {full_name} -> {dest}")
            run(["git", "clone", "--quiet", url, str(dest)])
        else:
            print(f"{full_name}: clone existente")
        for row in prs:
            ref = row["head"]
            if ref_exists(ref, str(dest)):
                continue
            print(f"  fetch {full_name} pull/{row['pr']}/head:{ref}")
            run(
                ["git", "fetch", "--quiet", "origin", f"pull/{row['pr']}/head:{ref}"],
                cwd=dest,
            )
        bases = sorted({row["base"] for row in prs})
        for base in bases:
            if ref_exists(base, str(dest)):
                continue
            # branch não-default só existe como origin/<base> num clone padrão
            if ref_exists(f"origin/{base}", str(dest)):
                run(["git", "branch", base, f"origin/{base}"], cwd=dest)
                continue
            run(["git", "fetch", "--quiet", "origin", base], cwd=dest)
            if not ref_exists(f"origin/{base}", str(dest)):
                raise SystemExit(f"{full_name}: base ref {base} ausente no clone")
            run(["git", "branch", base, f"origin/{base}"], cwd=dest)
        print(f"  ok: {len(prs)} PRs, bases {bases}")

    print("mrs50_clone: completo")


if __name__ == "__main__":
    sys.exit(main())
