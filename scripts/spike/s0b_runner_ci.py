"""Spike S0-B (P001-S006, resume 2026-09-25), células (a) no CE local:
ingestão via CI artifact (schema GitLab) e SARIF, com gitlab-runner do
docker compose (decisão do dono: gitlab.com adiado, células no CE local).

Fluxo:
1. Runner: token de autenticação via API (root) + registro one-shot em
   volume runner_config + verificação de status online.
2. Célula CI artifact: branch s0b/ci-artifact com .gitlab-ci.yml que
   produz gl-code-quality-report.json (schema GitLab) como artifact
   codequality; MR; pipeline; evidência = code_quality_diff do MR.
3. Célula SARIF: branch s0b/ci-sarif com codequality.sarif (SARIF 2.1.0)
   como artifact codequality; MR; pipeline; mesma evidência.
4. Limpeza: MRs fechados, branches deletadas (runner permanece).

Segredos: root PAT via cache do bootstrap (.lab/root-token); saída redigida
em results/p001-s006-s0b/runner-ci.json.

Uso:
    python3 scripts/spike/s0b_runner_ci.py [--url URL] [--out ARQUIVO]
        [--project-id 1] [--poll-seconds 600]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lab"))
from gitlab_bootstrap import _request
from s0b_oauth import load_lab, root_pat
from s0b_report import now_iso, write_evidence

RUNNER_NAME = "s0b-lab-runner"
RUNNER_IMAGE = "gitlab/gitlab-runner:v18.4.0"
COMPOSE_VOLUME = "gitlab_runner_config"
JOB_IMAGE = "ruby:3.3"
POLL_INTERVAL = 5.0
PIPELINE_STATES = ("created", "preparing", "pending", "waiting_for_resource", "running")

# Formato aceito pelo parser `codequality` do GitLab CE 18.4 (CodeClimate):
# ARRAY JSON de degradations, cada uma exigindo description, fingerprint,
# severity e location (schema app/validators/json_schemas/codeclimate.json).
# O objeto wrapped {"version","type","report":[...]} NAO e aceito (o parser
# faz root.each e espera o array direto) — verificado no rails runner.
CODEQUALITY_JSON = [
    {
        "description": "S0-B probe: SQL injection em handler de login",
        "check_name": "s0b-probe/sqli",
        "fingerprint": "s0b-probe-0001",
        "category": "Security",
        "severity": "major",
        "location": {"path": "app.py", "lines": {"begin": 10, "end": 12}},
        "message": "Possível SQL injection (probe S0-B, schema GitLab)",
    }
]

# O tipo `codequality` do CE 18.4 so parseia o formato CodeClimate (nao ha
# parser SARIF para codequality no CE/EE 18.4). A celula SARIF demonstra a
# ingestao "via conversor": o job escreve o SARIF (saida do scanner) e um
# conversor Ruby (imagem ruby:3.3) o transforma no array CodeClimate que o
# GitLab aceita. O artefato codequality aponta para o arquivo convertido.
SARIF_CONVERTER = (
    'ruby -e \'require "json"; require "digest"; '
    's=JSON.parse(File.read("codequality.sarif")); d=[]; '
    's["runs"].each{|run| run["results"].each{|r| '
    'loc=r.dig("locations",0,"physicalLocation")||{}; '
    'p=loc.dig("artifactLocation","uri"); l=loc.dig("region","startLine"); '
    'fp=Digest::SHA256.hexdigest("#{r["ruleId"]}:#{p}:#{l}"); '
    'd<<{"description"=>r.dig("message","text"),'
    '"check_name"=>r["ruleId"],"fingerprint"=>fp,'
    '"category"=>"Security",'
    '"severity"=>({"error"=>"critical","warning"=>"major",'
    '"note"=>"minor"}[r["level"]]||"info"),'
    '"location"=>{"path"=>p,"lines"=>{"begin"=>l}},'
    '"message"=>r.dig("message","text")}}}; '
    'File.write("gl-code-quality-report.json", d.to_json)\''
)

SARIF_JSON = {
    "$schema": "https://sarif.global/sarif-schema-2.1.0.json",
    "version": "2.1.0",
    "runs": [
        {
            "tool": {"driver": {"name": "s0b-probe", "version": "1.0"}},
            "results": [
                {
                    "ruleId": "s0b-probe/sqli",
                    "level": "error",
                    "message": {"text": "Possível SQL injection (probe S0-B, SARIF)"},
                    "locations": [
                        {
                            "physicalLocation": {
                                "artifactLocation": {"uri": "app.py"},
                                "region": {"startLine": 10},
                            }
                        }
                    ],
                }
            ],
        }
    ],
}


def _fail(message: str) -> None:
    print(f"s0b_runner_ci: {message}", file=sys.stderr)
    raise SystemExit(1)


def _write_report_cmd(report_file: str, report_json: str) -> str:
    # JSON em linha unica dentro de aspa simples (sem aspas simples no
    # conteudo) — heredoc multi-linha quebra o block scalar do YAML
    one_line = json.dumps(json.loads(report_json), separators=(",", ":"))
    return "printf '%s' '" + one_line + "' > " + report_file


def _ci_yaml(report_file: str, script_lines: list[str]) -> str:
    # cada linha e um comando shell separado no block scalar do YAML
    script_block = "".join("    - |\n      " + line + "\n" for line in script_lines)
    return (
        "s0b-report:\n"
        "  script:\n" + script_block + "  artifacts:\n"
        "    reports:\n"
        "      codequality: " + report_file + "\n"
    )


def _list_runners(base: str, token: str) -> list[dict[str, Any]]:
    # GET /api/v4/runners retorna lista direta (nao dict com chave runners)
    status, body = _request(f"{base}/api/v4/runners", token=token)
    if status != 200:
        return []
    return body if isinstance(body, list) else []


def _runner_online(base: str, token: str) -> dict[str, Any] | None:
    for runner in _list_runners(base, token):
        if runner.get("name") == RUNNER_NAME:
            return runner
    return None


def _apply_network_mode() -> None:
    """Injeta network_mode=host em [runners.docker] — o registro one-shot
    reescreve o config.toml a cada vez. Host porque a URL de clone do job
    e http://localhost:8929 (external_url) e o job container so alcana a
    porta publicada via rede host (Docker Desktop)."""
    proc = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "-v",
            f"{COMPOSE_VOLUME}:/etc/gitlab-runner",
            RUNNER_IMAGE,
            "-c",
            (
                "grep -q 'network_mode' /etc/gitlab-runner/config.toml || "
                "sed -i '/\\[runners.docker\\]/a\\    network_mode = \"host\"' "
                "/etc/gitlab-runner/config.toml; "
                "grep -c 'network_mode' /etc/gitlab-runner/config.toml"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        _fail(f"network_mode: {proc.stderr.strip()[:200]}")
    print(f"network_mode aplicado (linhas: {proc.stdout.strip()})")


def _ensure_service() -> None:
    """Sobe (ou reinicia, p/ pegar config nova) o serviço gitlab-runner.
    --env-file obrigatorio: o compose.yaml referencia variaveis (ex.
    GITLAB_ROOT_PASSWORD) que so existem no env do bootstrap."""
    env_file = str(Path(".lab/gitlab.env").resolve())
    cmd = [
        "docker",
        "compose",
        "-f",
        "lab/gitlab/compose.yaml",
        "--env-file",
        env_file,
        "up",
        "-d",
        "gitlab-runner",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
    if proc.returncode != 0:
        _fail(f"compose up gitlab-runner: {proc.stderr.strip()[:300]}")
    restart = [
        "docker",
        "compose",
        "-f",
        "lab/gitlab/compose.yaml",
        "--env-file",
        env_file,
        "restart",
        "gitlab-runner",
    ]
    proc = subprocess.run(
        restart, capture_output=True, text=True, timeout=300, check=False
    )
    if proc.returncode != 0:
        _fail(f"compose restart gitlab-runner: {proc.stderr.strip()[:300]}")


def register_runner(base: str, token: str) -> dict[str, Any]:
    runners = _list_runners(base, token)
    existing = next((r for r in runners if r.get("name") == RUNNER_NAME), None)
    if existing is not None and existing.get("status") == "online":
        print(f"runner já online: id={existing.get('id')}")
        _ensure_service()
        return existing
    if existing is not None:
        # órfão (criado, token perdido) — recria para obter novo token
        _request(
            f"{base}/api/v4/runners/{existing['id']}", method="DELETE", token=token
        )

    # órfãos com creation_state=started não aparecem na API — limpa no banco
    proc = subprocess.run(
        [
            "docker",
            "exec",
            "gitlab-gitlab-1",
            "gitlab-psql",
            "-c",
            "DELETE FROM ci_runners WHERE creation_state = 0;",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode == 0:
        print("orfaos limpos:", proc.stdout.strip()[-80:])

    status, body = _request(
        f"{base}/api/v4/user/runners",
        method="POST",
        token=token,
        payload={
            "runner_type": "instance_type",
            "name": RUNNER_NAME,
            "tag_list": ["s0b"],
            "access_level": "not_protected",
            "locked": False,
            "run_untagged": True,
        },
    )
    if status not in (200, 201):
        _fail(f"criação do runner: HTTP {status} {str(body)[:200]}")
    auth_token = body.get("token") if isinstance(body, dict) else None
    if not auth_token:
        _fail("runner criado sem campo token")

    # config.toml antigo acumula entradas [[runners]] de re-registros —
    # apaga antes para o registro criar a config limpa
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "-v",
            f"{COMPOSE_VOLUME}:/etc/gitlab-runner",
            RUNNER_IMAGE,
            "-c",
            "rm -f /etc/gitlab-runner/config.toml",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    cmd = [
        "docker",
        "run",
        "--rm",
        # rede host: a URL do registro e http://localhost:8929 (porta
        # publicada), inacessivel pela rede do compose
        "--network",
        "host",
        "-v",
        f"{COMPOSE_VOLUME}:/etc/gitlab-runner",
        RUNNER_IMAGE,
        # com token de autenticacao (glrt-), --tag-list e cia sao reservados
        # ao servidor (new_creation_workflow). URL e network_mode host:
        # o servico gitlab-runner roda em network_mode=host (compose.yaml)
        # e o job container herda network_mode=host do registro — ambos
        # alcancam o GitLab pela porta publicada http://localhost:8929
        # (a URL interna http://gitlab:80 nao e alcancavel fora da rede
        # do compose no Docker Desktop); o servidor guarda o valor e o
        # sync nao o apaga
        "register",
        "--url",
        "http://localhost:8929",
        "--token",
        auth_token,
        "--executor",
        "docker",
        "--docker-image",
        JOB_IMAGE,
        "--docker-network-mode",
        "host",
        "--non-interactive",
        "--name",
        RUNNER_NAME,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
    if proc.returncode != 0:
        _fail(f"registro do runner falhou: {proc.stderr.strip()[:300]}")

    _apply_network_mode()
    _ensure_service()

    # a API só lista o runner após o 1º build request (creation_state
    # started -> finished); o teste definitivo é o pipeline abaixo
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        runner = _runner_online(base, token)
        if runner is not None:
            return runner
        time.sleep(POLL_INTERVAL)
    print(
        "aviso: runner ainda não listado na API (creation_state=started "
        "até o 1º build) — seguindo para o pipeline"
    )
    return {
        "name": RUNNER_NAME,
        "id": body.get("id"),
        "status": "started (pré-primeiro-build)",
    }


def _push_ci_file(
    base: str, token: str, project_id: int, branch: str, ci_yaml: str
) -> int:
    status, body = _request(
        f"{base}/api/v4/projects/{project_id}/repository/branches",
        method="POST",
        token=token,
        payload={"branch": branch, "ref": "main"},
    )
    # 400 "Branch already exists" ok — o PUT abaixo reafirma o conteúdo
    if status not in (200, 201, 400, 409):
        _fail(f"branch {branch}: HTTP {status} {str(body)[:200]}")
    status, body = _request(
        f"{base}/api/v4/projects/{project_id}/repository/files/.gitlab-ci.yml",
        method="PUT",
        token=token,
        payload={
            "branch": branch,
            "content": ci_yaml,
            "commit_message": f"s0b: CI probe ({branch})",
        },
    )
    if status not in (200, 201):
        _fail(f".gitlab-ci.yml em {branch}: HTTP {status} {str(body)[:200]}")
    status, body = _request(
        f"{base}/api/v4/projects/{project_id}/merge_requests",
        method="POST",
        token=token,
        payload={
            "source_branch": branch,
            "target_branch": "main",
            "title": f"s0b probe {branch}",
        },
    )
    if status in (200, 201):
        return int(body["iid"])
    if status == 409:
        # MR aberto já existe para a branch (re-run) — reuso
        encoded = urllib.parse.quote(branch, safe="")
        status, mrs = _request(
            f"{base}/api/v4/projects/{project_id}/merge_requests"
            f"?source_branch={encoded}&state=opened",
            token=token,
        )
        if status == 200 and isinstance(mrs, list) and mrs:
            return int(mrs[0]["iid"])
    _fail(f"MR {branch}: HTTP {status} {str(body)[:200]}")


def _wait_pipeline(
    base: str, token: str, project_id: int, mr_iid: int, poll_seconds: float
) -> dict[str, Any]:
    deadline = time.monotonic() + poll_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        status, body = _request(
            f"{base}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/pipelines",
            token=token,
        )
        if status == 200 and isinstance(body, list) and body:
            last = body[0]
            if last.get("status") not in PIPELINE_STATES:
                return last
        time.sleep(POLL_INTERVAL)
    return last


def _diff_artifact(pipeline_id: int) -> dict[str, Any]:
    """Le o Ci::PipelineArtifact (file_type code_quality_mr_diff) da pipeline.

    A API do MR em CE 18.4 nao expoe o campo code_quality_diff — o diff e
    armazenado como artifact de pipeline (lido pelo presenter da UI). A
    evidencia concreta e o conteudo desse artifact (novos erros por arquivo).
    """
    script = (
        "a = Ci::PipelineArtifact.where(pipeline_id: "
        + str(pipeline_id)
        + ", file_type: :code_quality_mr_diff).first; "
        "if a; "
        "puts '@@EXISTS@@ ' + a.size.to_s; "
        "puts a.file.read; "
        "else; puts '@@MISSING@@'; end"
    )
    proc = subprocess.run(
        ["docker", "exec", "gitlab-gitlab-1", "gitlab-rails", "runner", script],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    lines = (proc.stdout or "").split("\n")
    for i, line in enumerate(lines):
        if line.startswith("@@EXISTS@@"):
            parts = line.split(" ")
            size = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
            content = "\n".join(lines[i + 1 :]).strip()
            try:
                content = json.loads(content)
            except json.JSONDecodeError:
                pass
            return {"exists": True, "size": size, "content": content}
        if line.strip() == "@@MISSING@@":
            return {"exists": False}
    return {"exists": False, "raw": (proc.stdout or proc.stderr or "")[-300:]}


def _wait_diff_artifact(pipeline_id: int, poll_seconds: float) -> dict[str, Any]:
    """Espera o worker assincrono (CreateQualityReportWorker) criar o diff.

    O worker e enfileirado no fim da pipeline; sem esta espera o _cell fecha
    o MR antes do diff existir (corrida). Intervalo 20s, timeout bounded.
    """
    if not pipeline_id:
        return {"exists": False, "note": "pipeline id ausente"}
    deadline = time.monotonic() + poll_seconds
    result = _diff_artifact(pipeline_id)
    while not result.get("exists") and time.monotonic() < deadline:
        time.sleep(20)
        result = _diff_artifact(pipeline_id)
    return result


def _cell(
    base: str,
    token: str,
    project_id: int,
    branch: str,
    report_file: str,
    script_lines: list[str],
    poll_seconds: float,
) -> dict[str, Any]:
    mr_iid = _push_ci_file(
        base, token, project_id, branch, _ci_yaml(report_file, script_lines)
    )
    pipeline = _wait_pipeline(base, token, project_id, mr_iid, poll_seconds)
    pipeline_id = pipeline.get("id")
    # espera o diff artifact (worker assincrono) ANTES de fechar o MR
    diff = _wait_diff_artifact(pipeline_id, poll_seconds)
    _status, mr = _request(
        f"{base}/api/v4/projects/{project_id}/merge_requests/{mr_iid}",
        token=token,
    )
    mr_body = mr if isinstance(mr, dict) else {}
    evidence = {
        "branch": branch,
        "mr_iid": mr_iid,
        "pipeline": {
            "id": pipeline_id,
            "status": pipeline.get("status"),
            "sha": pipeline.get("sha"),
        },
        "code_quality_mr_diff": diff,
        "mr_state": mr_body.get("state"),
    }
    # limpeza (branch com '/' precisa de URL-encoding)
    _request(
        f"{base}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/close",
        method="PUT",
        token=token,
    )
    encoded = urllib.parse.quote(branch, safe="")
    _request(
        f"{base}/api/v4/projects/{project_id}/repository/branches/{encoded}",
        method="DELETE",
        token=token,
    )
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url")
    parser.add_argument("--out", default="results/p001-s006-s0b/runner-ci.json")
    parser.add_argument("--project-id", type=int, default=1)
    parser.add_argument("--poll-seconds", type=float, default=600)
    args = parser.parse_args()

    lab = load_lab(Path(".lab"))
    base = args.url or lab["url"]
    token = root_pat(base, "gitlab-gitlab-1", Path(".lab"))

    runner = register_runner(base, token)
    print(
        f"runner: id={runner.get('id')} status={runner.get('status')} "
        f"version={runner.get('version')}"
    )

    cells = {
        # celula 1: report ja no formato CodeClimate (o que o GitLab aceita)
        "ci_artifact_gl_schema": _cell(
            base,
            token,
            args.project_id,
            "s0b/ci-artifact",
            "gl-code-quality-report.json",
            [
                _write_report_cmd(
                    "gl-code-quality-report.json",
                    json.dumps(CODEQUALITY_JSON, indent=2),
                )
            ],
            args.poll_seconds,
        ),
        # celula 2: SARIF (saida do scanner) -> conversor Ruby -> CodeClimate
        "ci_artifact_sarif": _cell(
            base,
            token,
            args.project_id,
            "s0b/ci-sarif",
            "gl-code-quality-report.json",
            [
                _write_report_cmd(
                    "codequality.sarif",
                    json.dumps(SARIF_JSON, indent=2),
                ),
                SARIF_CONVERTER,
            ],
            args.poll_seconds,
        ),
    }

    write_evidence(
        Path(args.out),
        {
            "generated_at": now_iso(),
            "scope": "resume 2026-09-25: celulas (a) CI artifact/SARIF no CE local "
            "(decisao do dono: gitlab.com adiado)",
            "runner": {
                "name": runner.get("name"),
                "id": runner.get("id"),
                "status": runner.get("status"),
                "version": runner.get("version"),
                "registration": "one-shot docker run + volume runner_config "
                "(token de autenticacao usado uma vez)",
            },
            "cells": cells,
        },
    )
    print(f"evidencia em {args.out}")


if __name__ == "__main__":
    main()
