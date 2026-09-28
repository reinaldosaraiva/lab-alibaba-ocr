"""Spike S0-B (P001-S006), entregável (a): evidência de tier CE para a
ingestão no Security nativo + comportamento de pipeline sem runner.

Probes dos endpoints de Security com o bot_token (403 "not licensed"/404
esperados — Vulnerability Report e ingestão nativa são Ultimate-only no CE;
a resposta É a evidência de tier). Pipeline de teste: o sandbox lab/sandbox
nasce vazio (o bootstrap só cria grupo/projeto/bot), então este probe semeia
o branch default com um .gitlab-ci.yml mínimo — sem config de CI o POST
/pipeline responde 422; com config, o bot pode ainda levar 403
(permissão de pipeline) — ambos os caminhos ficam registrados como
evidência honesta da célula "CI artifact no CE" (mecanismo real só
no GitLab.com, que exige runner + tier Ultimate para Security). O
seeding tenta primeiro como bot (Developer; o resultado fica registrado como
evidência) e cai para o PAT root se a proteção de branch default bloquear.
O pipeline NUNCA fica rodando: poll <= --poll-seconds e cancel no fim;
branch temporária s0b/ci-probe é deletada quando usada.

Segredos: bot_token lido de .lab/gitlab.json e usado só em memória; saída
redigida (redact) em results/p001-s006-s0b/ce-probe.json.

Uso:
    python3 scripts/spike/s0b_ce_probe.py [--url URL] [--out ARQUIVO]
        [--poll-seconds 60] [--container gitlab-gitlab-1]
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lab"))
from gitlab_bootstrap import _request
from s0b_oauth import _error_message, load_lab, root_pat
from s0b_report import now_iso, timed, write_evidence

SECURITY_ENDPOINTS = (
    "vulnerabilities",
    "security_findings",
    "vulnerability_findings",
    "scans",
)
CI_YAML = 's0b:\n  script:\n    - echo "s0b pipeline probe"\n'
PROBE_BRANCH = "s0b/ci-probe"
SEED_FILE = "s0b-seed.md"
POLL_INTERVAL = 5.0
WAITING_STATES = ("created", "preparing", "pending", "waiting_for_resource", "queued")
LAB_DIR = Path(".lab")
OUT = Path("results/p001-s006-s0b/ce-probe.json")
DEFAULT_CONTAINER = "gitlab-gitlab-1"


def _fail(message: str) -> None:
    print(f"s0b_ce_probe: {message}", file=sys.stderr)
    raise SystemExit(1)


def _commit(
    base_url: str,
    pid: int,
    token: str,
    branch: str,
    message: str,
    actions: list[dict[str, str]],
    start_branch: str | None = None,
) -> tuple[int, Any]:
    """Commit via API; em repo vazio o commit cria o branch informado."""
    payload: dict[str, Any] = {
        "branch": branch,
        "commit_message": message,
        "actions": actions,
    }
    if start_branch:
        payload["start_branch"] = start_branch
    return _request(
        f"{base_url}/api/v4/projects/{pid}/repository/commits",
        method="POST",
        token=token,
        payload=payload,
    )


def _has_ci_config(base_url: str, pid: int, token: str, ref: str) -> bool:
    filepath = urllib.parse.quote(".gitlab-ci.yml", safe="")
    quoted_ref = urllib.parse.quote(ref, safe="")
    code, _ = _request(
        f"{base_url}/api/v4/projects/{pid}/repository/files/{filepath}"
        f"?ref={quoted_ref}",
        token=token,
    )
    return code == 200


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=None, help="default: url do .lab/gitlab.json")
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--container", default=DEFAULT_CONTAINER)
    parser.add_argument("--poll-seconds", type=float, default=60.0)
    args = parser.parse_args()

    lab = load_lab(LAB_DIR)
    base = (args.url or lab["url"]).rstrip("/")
    pid = int(lab["project"]["id"])
    bot = str(lab["bot_token"])
    bot_username = str((lab.get("bot") or {}).get("username") or "ocr-bot")
    try:
        _request(f"{base}/api/v4/version", token=bot)
    except OSError:
        _fail(f"GitLab inacessível em {base} (make lab-up)")

    root_cache: dict[str, str] = {}

    def ensure_root() -> str:
        if "pat" not in root_cache:
            root_cache["pat"] = root_pat(base, args.container, LAB_DIR)
        return root_cache["pat"]

    timings: dict[str, float] = {}
    probes: list[dict[str, Any]] = []
    pipeline_result: dict[str, Any] = {}
    result: dict[str, Any] = {
        "session": "P001-S006",
        "generated_at": now_iso(),
        "gitlab": {"version": None, "tier": "CE"},
        "security_endpoints": probes,
        "pipeline": pipeline_result,
        "runners": None,
        "timings": timings,
    }

    def abort(message: str) -> None:
        result["error"] = message
        _fail(message)

    try:
        # 1. probes de Security — 403/404 esperados como evidência de tier
        for name in SECURITY_ENDPOINTS:
            with timed(timings, f"probe_{name}"):
                code, body = _request(f"{base}/api/v4/projects/{pid}/{name}", token=bot)
            probes.append(
                {
                    "endpoint": f"/api/v4/projects/{pid}/{name}",
                    "status": code,
                    "message": _error_message(body),
                }
            )
        # versão exige admin: root (cache/mint) quando o bot leva 403
        code, body = _request(f"{base}/api/v4/version", token=bot)
        if code != 200:
            code, body = _request(f"{base}/api/v4/version", token=ensure_root())
        if code == 200 and isinstance(body, dict):
            result["gitlab"]["version"] = body.get("version")

        # 2. pipeline de teste — ref default; sandbox vazio é semeado antes
        code, project = _request(f"{base}/api/v4/projects/{pid}", token=bot)
        default = (
            project.get("default_branch")
            if code == 200 and isinstance(project, dict)
            else None
        )
        seeded = False
        seed_identity = None
        if code == 200 and not default:
            actions = [
                {
                    "action": "create",
                    "file_path": ".gitlab-ci.yml",
                    "content": CI_YAML,
                },
                {
                    "action": "create",
                    "file_path": SEED_FILE,
                    "content": f"S0-B seed ({now_iso()}) — probe de pipeline.\n",
                },
            ]
            message = "s0b: seed inicial com .gitlab-ci.yml para probe"
            with timed(timings, "seed_initial_commit"):
                code_seed, body_seed = _commit(base, pid, bot, "main", message, actions)
            if code_seed not in (200, 201):
                code_seed, body_seed = _commit(
                    base, pid, ensure_root(), "main", message, actions
                )
                if code_seed not in (200, 201):
                    abort(
                        f"seed do branch default falhou (HTTP {code_seed}):"
                        f" {_error_message(body_seed)}"
                    )
                seed_identity = "root"
            else:
                seed_identity = bot_username
            seeded = True
            default = "main"
        pipeline_result["default_branch"] = default
        pipeline_result["seeded_initial_commit"] = seeded
        pipeline_result["seed_identity"] = seed_identity

        ref = default
        branch_creator: str | None = None
        if default and not _has_ci_config(base, pid, bot, default):
            # sem config de CI o POST /pipeline falha 422: branch dedicada
            # com config mínima, deletada na limpeza (não mexe no default)
            actions = [
                {
                    "action": "create",
                    "file_path": ".gitlab-ci.yml",
                    "content": CI_YAML,
                }
            ]
            message = "s0b: config de CI mínima para probe de pipeline"
            with timed(timings, "ci_branch_commit"):
                code_ci, body_ci = _commit(
                    base,
                    pid,
                    bot,
                    PROBE_BRANCH,
                    message,
                    actions,
                    start_branch=default,
                )
            branch_creator = bot
            if code_ci not in (200, 201):
                code_ci, body_ci = _commit(
                    base,
                    pid,
                    ensure_root(),
                    PROBE_BRANCH,
                    message,
                    actions,
                    start_branch=default,
                )
                branch_creator = ensure_root()
                if code_ci not in (200, 201):
                    abort(
                        f"branch {PROBE_BRANCH} falhou (HTTP {code_ci}):"
                        f" {_error_message(body_ci)}"
                    )
            ref = PROBE_BRANCH
        pipeline_result["ref"] = ref
        pipeline_result["ci_config_branch"] = ref

        with timed(timings, "pipeline_create"):
            code, body = _request(
                f"{base}/api/v4/projects/{pid}/pipeline",
                method="POST",
                token=bot,
                payload={"ref": ref},
            )
        if code in (200, 201) and isinstance(body, dict):
            pipe_id = int(body["id"])
            pipeline_result["created"] = {
                "http": code,
                "id": pipe_id,
                "status": body.get("status"),
                "web_url": body.get("web_url"),
            }
            samples: list[dict[str, Any]] = [
                {"elapsed_s": 0.0, "status": body.get("status")}
            ]
            started = time.monotonic()
            deadline = started + args.poll_seconds
            while time.monotonic() < deadline:
                time.sleep(POLL_INTERVAL)
                code, body = _request(
                    f"{base}/api/v4/projects/{pid}/pipelines/{pipe_id}", token=bot
                )
                status_now = (
                    body.get("status")
                    if code == 200 and isinstance(body, dict)
                    else None
                )
                samples.append(
                    {
                        "elapsed_s": round(time.monotonic() - started, 1),
                        "status": status_now,
                    }
                )
                if status_now not in WAITING_STATES:
                    break
            pipeline_result["poll_samples"] = samples
            with timed(timings, "pipeline_cancel"):
                code, body = _request(
                    f"{base}/api/v4/projects/{pid}/pipelines/{pipe_id}/cancel",
                    method="POST",
                    token=bot,
                )
            pipeline_result["cancel"] = {
                "http": code,
                "status_after": (
                    body.get("status")
                    if code == 200 and isinstance(body, dict)
                    else None
                ),
            }
            if branch_creator:
                quoted = urllib.parse.quote(PROBE_BRANCH, safe="")
                code, _ = _request(
                    f"{base}/api/v4/projects/{pid}/repository/branches/{quoted}",
                    method="DELETE",
                    token=branch_creator,
                )
                pipeline_result["branch_cleanup"] = f"{PROBE_BRANCH} (HTTP {code})"
            else:
                pipeline_result["branch_cleanup"] = "default mantido"
        else:
            pipeline_result["created"] = {
                "http": code,
                "message": _error_message(body),
            }
            pipeline_result["branch_cleanup"] = (
                f"{PROBE_BRANCH} não deletada (pipeline não criada)"
                if branch_creator
                else "default mantido"
            )

        code, body = _request(f"{base}/api/v4/projects/{pid}/runners", token=bot)
        result["runners"] = {
            "project_runners": (
                len(body) if code == 200 and isinstance(body, list) else None
            ),
            "note": "container CE sem binário gitlab-runner; pipeline fica pending",
        }
    finally:
        write_evidence(args.out, result)

    statuses = [entry["status"] for entry in probes]
    created = pipeline_result.get("created") or {}
    cancel = pipeline_result.get("cancel") or {}
    print(
        f"s0b_ce_probe: security {statuses}; pipeline"
        f" {created.get('id')} → {cancel.get('status_after')};"
        f" evidência em {args.out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
