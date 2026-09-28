"""Driver SaaS: MR → review → fix → testes/pre-commit → push → aprovação.

Fluxo ideal (dono, 2026-09-28): o MR abre, um bot revisa com comentários, e
OUTRO modelo corrige a partir dos comentários — iterando até o diff ficar
limpo ou esgotar os ciclos. O re-review de cada ciclo é a rede de segurança
que pega resíduos da substituição de código (validado no MR !23 do sandbox:
o ciclo 1 deixou código morto e o re-review do ciclo 2 pegou). Cada fix
ainda passa por um gate sintático (ruff F821) antes do commit — um fix que
introduza erro estático, como usar um nome não importado (caso real do MR
!25), é revertido na hora e nunca chega ao push.

Papéis e identidades (dois providers):
- revisor: motor OCR (open-code-review) com provider profundo — default
  deepseek/deepseek-v4-pro (pago por token, análise funda);
- fixer: two_provider.fix_finding com provider de baixo custo — default
  magalu/qwen38-27b (zero custo);
- git ops (branch, commit de fix, push): commits de GITLAB_BOT_USERNAME
  (fixer); push pelo token do origin do clone;
- reviewer do MR: GITLAB_REVIEWER_USERNAME, conta distinta do fixer,
  atribuída no campo Reviewer; publica findings e aprova o SHA revisado;
- ambos usam tokens OAuth G6 transitórios para suas notas; nenhum bot
  precisa de PAT permanente para comentários ou aprovação.
- pipeline CI: API com credencial administrativa do lab (ocr-bot recebe 403
  nesse endpoint no GitLab CE); não usada para comentários nem commits.

Uso:
    python3 scripts/saas/autofix_loop.py --repo CLONE --branch saas/x \
        [--target develop] [--title T] [--reviewer deepseek] \
        [--review-model deepseek-v4-pro] [--effort high] \
        [--max-tokens-budget 100000] [--fixer magalu] [--max-cycles 3] \
        --test-cmd 'python3 -m unittest discover -s scripts/saas -p test_*.py' \
        [--auto-merge]
    python3 scripts/saas/autofix_loop.py --repo CLONE --mr-iid 23 [...]

O clone (--repo) precisa de origin com push configurado (o sandbox do lab
já tem o token do bot embutido na URL).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "spike"))

import s0b_oauth as o  # depois do sys.path por design
import two_provider  # depois do sys.path por design

REPO_ROOT = Path(__file__).resolve().parents[2]
OCR_BIN = REPO_ROOT / "open-code-review" / "dist" / "opencodereview"
REVIEW_RULE = REPO_ROOT / "scripts" / "saas" / "review_rule.json"
APP_NAME = "saas-autofix-loop"

DEFAULT_BACKGROUND = (
    "SaaS de AI code review GitLab-first (lab-alibaba-ocr). Revise o diff e aponte "
    "bugs, riscos de segurança e problemas de qualidade — um finding por problema, "
    "com severidade, explicação e sugestão de correção."
)
REREVIEW_SUFFIX = (
    " Este é um re-review após correções aplicadas por um modelo corretor: verifique "
    "se as correções estão completas e corretas, em particular resíduos de "
    "substituição de código (blocos mortos, duplicação, código inalcançável)."
)
TEST_IMAGE = "python:3.12@sha256:4d1caded1f729ae443eb803f26ffde7b61e696aeaef62f099abb6dd6b14257c7"
PIPELINE_TIMEOUT_SECONDS = 300


def git(repo: Path, *args: str) -> tuple[int, str]:
    r = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    )
    return r.returncode, (r.stdout + r.stderr).strip()


def parse_code_block(text: str) -> str | None:
    """Extrai o último bloco de código cercado (python ou neutro); None sem blocos."""
    blocks = re.findall(r"```(?:python)?\n(.*?)```", text, re.DOTALL)
    return blocks[-1].rstrip("\n") if blocks else None


def apply_patch(src: str, finding: dict[str, Any], block: str | None) -> str | None:
    """Substitui o existing_code do finding pelo bloco corrigido.

    None quando o finding não é aplicável (sem bloco, sem existing_code ou
    trecho não encontrado no arquivo).
    """
    if not block:
        return None
    old = finding.get("existing_code") or ""
    if not old or src.count(old) != 1:
        return None
    return src.replace(old, block, 1)


def should_continue(
    cycle: int, findings: list[dict[str, Any]], max_cycles: int
) -> bool:
    """O loop continua enquanto há findings e ciclos restantes."""
    return bool(findings) and cycle < max_cycles


def run_review(
    repo: Path,
    from_sha: str,
    to_sha: str,
    reviewer: str,
    review_model: str,
    background: str,
    effort: str = "medium",
    max_tokens_budget: int = 0,
    timeout_seconds: int = 1800,
) -> dict[str, Any]:
    """Uma rodada do motor OCR (JSON) entre dois SHAs (modo merge-base)."""
    cmd = [
        str(OCR_BIN),
        "review",
        "--from",
        from_sha,
        "--to",
        to_sha,
        "--repo",
        str(repo.resolve()),
        "--format",
        "json",
        "--audience",
        "agent",
        "--effort",
        effort,
        "--provider",
        reviewer,
        "--model",
        review_model,
        "--rule",
        str(REVIEW_RULE),
        "--background",
        background,
    ]
    if max_tokens_budget:
        cmd.extend(["--max-tokens-budget", str(max_tokens_budget)])
    review_env = os.environ.copy()
    if reviewer != "magalu":
        # O binário do lab troca o trust store inteiro quando esta variável
        # está presente; o endpoint DeepSeek precisa das raízes do sistema.
        review_env.pop("OCR_ROOT_CA_FILE", None)
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
        env=review_env,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"autofix_loop: review falhou (exit {proc.returncode}): "
            f"{(proc.stderr or '')[-300:]}"
        )
    return json.loads(proc.stdout.strip())


def review_preview(
    repo: Path,
    from_sha: str,
    to_sha: str,
    reviewer: str,
    model: str,
    effort: str = "medium",
    max_tokens_budget: int = 0,
) -> list[dict[str, Any]]:
    """Resolve a seleção de arquivos com a mesma regra confiável do review."""
    cmd = [
        str(OCR_BIN),
        "review",
        "--from",
        from_sha,
        "--to",
        to_sha,
        "--repo",
        str(repo.resolve()),
        "--format",
        "json",
        "--audience",
        "agent",
        "--effort",
        effort,
        "--provider",
        reviewer,
        "--model",
        model,
        "--rule",
        str(REVIEW_RULE),
        "--preview",
    ]
    if max_tokens_budget:
        cmd.extend(["--max-tokens-budget", str(max_tokens_budget)])
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    if proc.returncode:
        raise RuntimeError(f"preview do OCR falhou: {(proc.stderr or '')[-300:]}")
    files = json.loads(proc.stdout).get("files")
    if not isinstance(files, list):
        raise TypeError("preview do OCR sem lista de arquivos")
    return files


def validate_review_result(
    review: dict[str, Any],
    preview: list[dict[str, Any]],
    from_sha: str,
    to_sha: str,
    provider: str,
    model: str,
) -> list[dict[str, Any]]:
    """Aceita findings apenas de uma revisão completa do SHA e seleção esperados."""
    manifest = review.get("manifest") or {}
    coverage = manifest.get("coverage") or {}
    selected = coverage.get("selected") or []
    completed = coverage.get("completed") or []
    reused = coverage.get("reused") or []
    input_ref = manifest.get("input") or {}
    execution = manifest.get("execution") or {}
    blocked = [
        f"{item.get('path')} ({item.get('exclude_reason')})"
        for item in preview
        if not item.get("will_review")
        and item.get("exclude_reason") not in {"unsupported_ext", "deleted"}
    ]
    expected = {item.get("path") for item in preview if item.get("will_review")}
    reviewed = {item.get("path") for item in selected}
    if (
        review.get("status") != "complete"
        or manifest.get("schema_version") != "ocr.run-manifest/v1"
        or manifest.get("operation") != "review"
        or manifest.get("terminal_state") != "complete"
        or manifest.get("run_failure")
        or not expected
        or blocked
        or reviewed != expected
        or len(selected) != len(completed) + len(reused)
        or coverage.get("failed")
        or coverage.get("waived")
        or input_ref.get("requested_from") != from_sha
        or input_ref.get("mode") != "range"
        or input_ref.get("requested_head") != to_sha
        or input_ref.get("resolved_head") != to_sha
        or execution.get("provider") != provider
        or execution.get("model") != model
        or (review.get("llm") or {}).get("provider") != provider
        or (review.get("llm") or {}).get("model") != model
    ):
        raise RuntimeError(
            "review incompleto ou seleção/modelo/SHA divergente: "
            f"status={review.get('status')}, selecionados={len(selected)}, "
            f"esperados={len(expected)}, excluídos={blocked[:5]}"
        )
    comments = review.get("comments")
    if not isinstance(comments, list):
        raise TypeError("review completo sem lista de comentários")
    if any(
        not isinstance(item, dict) or item.get("path") not in reviewed
        for item in comments
    ):
        raise RuntimeError("finding aponta arquivo fora da cobertura revisada")
    return comments


def api_request(
    base: str,
    path: str,
    token: str,
    payload: dict[str, Any] | None = None,
    method: str = "GET",
) -> tuple[int, Any]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{base}{path}",
        data=data,
        method=method,
        headers={"PRIVATE-TOKEN": token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode())
        except (OSError, ValueError):
            return exc.code, {}


def bot_identities() -> dict[str, dict[str, str]]:
    """Carrega duas contas distintas antes de criar ou alterar um MR."""
    o.load_lab_env(o.LAB_DIR)
    identities = {
        "reviewer": {
            "user": os.environ.get("GITLAB_REVIEWER_USERNAME", ""),
            "password": os.environ.get("GITLAB_REVIEWER_PASSWORD", ""),
        },
        "fixer": {
            "user": os.environ.get("GITLAB_BOT_USERNAME", ""),
            "password": os.environ.get("GITLAB_BOT_PASSWORD", ""),
        },
    }
    if any(
        not identity["user"] or not identity["password"]
        for identity in identities.values()
    ):
        raise SystemExit(
            "autofix_loop: credenciais de reviewer/fixer ausentes (.lab/gitlab.env)"
        )
    if identities["reviewer"]["user"] == identities["fixer"]["user"]:
        raise SystemExit("autofix_loop: reviewer e fixer exigem contas distintas")
    return identities


def g6_open(base: str, identities: dict[str, dict[str, str]]) -> dict[str, Any]:
    """Uma aplicação G6 com grants OAuth transitórios para os dois bots."""
    pat = o.root_pat(base, o.DEFAULT_CONTAINER, o.LAB_DIR)
    o.delete_stale_applications(base, pat, APP_NAME)
    app = o.create_application(base, pat, APP_NAME, o.REDIRECT_URI, o.APP_SCOPES)
    session: dict[str, Any] = {"pat": pat, "app": app, "tokens": {}}
    try:
        for role, identity in identities.items():
            code, grant = o.password_grant(
                base, app, identity["user"], identity["password"], o.APP_SCOPES
            )
            if code != 200 or not grant.get("access_token"):
                raise RuntimeError(
                    f"autofix_loop: OAuth do {role} falhou (HTTP {code})"
                )
            session["tokens"][role] = grant["access_token"]
    except Exception:
        g6_close(base, session)
        raise
    return session


def g6_close(base: str, session: dict[str, Any]) -> None:
    failures: list[str] = []
    for role, token in session["tokens"].items():
        try:
            code = o.revoke_token(base, session["app"], token)
            if code not in (200, 204):
                failures.append(f"revogação {role}: HTTP {code}")
        except OSError as exc:
            failures.append(f"revogação {role}: {type(exc).__name__}")
    try:
        code = o.delete_application(base, session["pat"], int(session["app"]["id"]))
        if code not in (200, 204):
            failures.append(f"exclusão do app: HTTP {code}")
    except OSError as exc:
        failures.append(f"exclusão do app: {type(exc).__name__}")
    if failures:
        raise RuntimeError("autofix_loop: limpeza OAuth falhou: " + "; ".join(failures))


def post_note(base: str, pid: int, iid: int, tok: str, body: str) -> None:
    code, _ = o.bearer_request(
        f"{base}/api/v4/projects/{pid}/merge_requests/{iid}/notes",
        method="POST",
        token=tok,
        payload={"body": body},
    )
    if code not in (200, 201):
        raise RuntimeError(f"nota no MR !{iid} falhou: HTTP {code}")


def reviewer_user_id(base: str, token: str, username: str) -> int:
    """Resolve a conta que aparecerá no campo Reviewer do MR."""
    code, users = api_request(
        base, f"/api/v4/users?username={urllib.parse.quote(username)}", token
    )
    if code != 200 or not isinstance(users, list):
        raise RuntimeError(
            f"não foi possível localizar o reviewer {username}: HTTP {code}"
        )
    for user in users:
        if user.get("username") == username:
            return int(user["id"])
    raise RuntimeError(f"reviewer {username} não encontrado no GitLab")


def assign_reviewer(base: str, pid: int, iid: int, token: str, user_id: int) -> None:
    """Atribui o bot como Reviewer, preservando outros revisores do MR."""
    path = f"/api/v4/projects/{pid}/merge_requests/{iid}"
    code, mr = api_request(base, path, token)
    if code != 200 or mr.get("state") != "opened":
        raise RuntimeError(f"MR !{iid} não está aberto para atribuir reviewer")
    reviewer_ids = [int(user["id"]) for user in mr.get("reviewers", [])]
    if user_id not in reviewer_ids:
        code, _ = api_request(
            base, path, token, {"reviewer_ids": [*reviewer_ids, user_id]}, "PUT"
        )
        if code != 200:
            raise RuntimeError(
                f"atribuição do reviewer no MR !{iid} falhou: HTTP {code}"
            )
        code, mr = api_request(base, path, token)
        if code != 200:
            raise RuntimeError(f"MR !{iid} não pôde ser relido após atribuição")
    if user_id not in {int(user["id"]) for user in mr.get("reviewers", [])}:
        raise RuntimeError(f"reviewer não aparece no MR !{iid} após atribuição")


def approve_review(
    base: str, pid: int, iid: int, tok: str, reviewed_sha: str, reviewer_user: str
) -> None:
    """Assina o SHA limpo via OAuth e confirma a aprovação do bot no GitLab."""
    path = f"{base}/api/v4/projects/{pid}/merge_requests/{iid}"
    code, result = o.bearer_request(
        path + "/approve", method="POST", token=tok, payload={"sha": reviewed_sha}
    )
    if code not in (200, 201):
        raise RuntimeError(
            f"aprovação do MR !{iid} falhou: HTTP {code} {str(result)[:200]}"
        )
    code, approvals = o.bearer_request(path + "/approvals", token=tok)
    signed = (
        code == 200
        and approvals.get("approved") is True
        and any(
            item.get("user", {}).get("username") == reviewer_user
            for item in approvals.get("approved_by", [])
        )
    )
    if not signed:
        raise RuntimeError(
            f"aprovação do MR !{iid} não foi registrada para {reviewer_user}"
        )


def trigger_pipeline(base: str, pid: int, branch: str, token: str) -> int:
    """Cria pipeline da branch explicitamente (push do bot não a dispara no CE)."""
    code, result = api_request(
        base,
        f"/api/v4/projects/{pid}/pipeline",
        token,
        {"ref": branch},
        "POST",
    )
    if code not in (200, 201):
        raise RuntimeError(
            f"pipeline de {branch} falhou: HTTP {code} {str(result)[:200]}"
        )
    return int(result["id"])


def require_trusted_ci(repo: Path, target: str, branch: str) -> None:
    """A fonte não pode trocar a definição de CI aprovada na branch alvo."""
    contents = []
    for ref in (target, branch):
        proc = subprocess.run(
            ["git", "show", f"origin/{ref}:.gitlab-ci.yml"],
            cwd=repo,
            capture_output=True,
            check=False,
        )
        if proc.returncode:
            raise RuntimeError(f"CI confiável ausente em origin/{ref}")
        contents.append(proc.stdout)
    if contents[0] != contents[1]:
        raise RuntimeError("branch alterou .gitlab-ci.yml; revisão humana necessária")


def assert_refs_unchanged(
    repo: Path, target: str, branch: str, base_sha: str, head_sha: str
) -> None:
    code, fetched = git(repo, "fetch", "origin")
    if code:
        raise RuntimeError(f"fetch de verificação falhou: {fetched[-300:]}")
    for ref, expected in ((target, base_sha), (branch, head_sha)):
        code, current = git(repo, "rev-parse", f"origin/{ref}")
        if code or current != expected:
            raise RuntimeError(f"origin/{ref} mudou após o review")


def wait_pipeline(base: str, pid: int, pipeline_id: int, sha: str, token: str) -> None:
    """Exige sucesso da pipeline disparada para o SHA que foi revisado."""
    deadline = time.monotonic() + PIPELINE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        code, pipeline = api_request(
            base, f"/api/v4/projects/{pid}/pipelines/{pipeline_id}", token
        )
        if code != 200 or not isinstance(pipeline, dict):
            raise RuntimeError(f"pipeline #{pipeline_id} inacessível: HTTP {code}")
        if pipeline.get("sha") != sha:
            raise RuntimeError(f"pipeline #{pipeline_id} não pertence ao SHA revisado")
        status = pipeline.get("status")
        if status == "success":
            return
        if status not in {
            "created",
            "pending",
            "preparing",
            "waiting_for_resource",
            "running",
        }:
            raise RuntimeError(f"pipeline #{pipeline_id} terminou com status {status}")
        time.sleep(3)
    raise RuntimeError(f"pipeline #{pipeline_id} excedeu {PIPELINE_TIMEOUT_SECONDS}s")


def changed_files(repo: Path, base_sha: str | None = None) -> list[str]:
    """Lê nomes literais, inclusive Unicode e quebras de linha, via NUL."""
    cmd = ["git", "diff", "--name-only", "-z", "--diff-filter=ACMR"]
    cmd.extend([f"{base_sha}..HEAD"] if base_sha else ["--cached"])
    proc = subprocess.run(cmd, cwd=repo, capture_output=True, check=False)
    if proc.returncode:
        raise RuntimeError(f"git diff falhou: {os.fsdecode(proc.stderr)[-300:]}")
    return [os.fsdecode(part) for part in proc.stdout.split(b"\0") if part]


def isolated_tests(repo: Path, tree: str, test_cmd: str) -> None:
    """Testa snapshot Git em container sem rede, segredos, .git ou host mounts."""
    with tempfile.TemporaryDirectory(prefix="saas-check-", dir=repo.parent) as tmp:
        snapshot = Path(tmp) / "source"
        snapshot.mkdir()
        archive = Path(tmp) / "source.tar"
        exported = subprocess.run(
            ["git", "archive", "--format=tar", "--output", str(archive), tree],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if exported.returncode:
            raise RuntimeError(f"snapshot para testes falhou: {exported.stderr[-300:]}")
        extracted = subprocess.run(
            ["tar", "-xf", str(archive), "-C", str(snapshot)],
            capture_output=True,
            text=True,
            check=False,
        )
        if extracted.returncode:
            raise RuntimeError(
                f"extração do snapshot falhou: {extracted.stderr[-300:]}"
            )
        checks = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "128",
                "--memory",
                "1g",
                "--cpus",
                "2",
                "--user",
                "65534:65534",
                "--tmpfs",
                "/tmp:rw,exec,size=128m",
                "--mount",
                f"type=bind,source={snapshot},target=/work,readonly",
                "--workdir",
                "/work",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
                TEST_IMAGE,
                *shlex.split(test_cmd),
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if checks.returncode:
            raise RuntimeError(
                f"testes isolados falharam: {(checks.stdout + checks.stderr)[-500:]}"
            )


def trusted_precommit(repo: Path, files: list[str]) -> None:
    if not files:
        raise RuntimeError("nenhum arquivo alterado para pre-commit")
    env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "LANG", "LC_ALL")
        if key in os.environ
    }
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    hooks = subprocess.run(
        [
            "pre-commit",
            "run",
            "--config",
            str(REPO_ROOT / ".pre-commit-config.yaml"),
            "--files",
            *files,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if hooks.returncode:
        raise RuntimeError(f"pre-commit falhou: {(hooks.stdout + hooks.stderr)[-500:]}")


def validate_head(repo: Path, base_sha: str, head_sha: str, test_cmd: str) -> None:
    """Executa testes e hooks sobre todo o diff final, inclusive sem fix do bot."""
    code, local_head = git(repo, "rev-parse", "HEAD")
    if code or local_head != head_sha:
        raise RuntimeError("worktree não está no SHA revisado")
    isolated_tests(repo, head_sha, test_cmd)
    trusted_precommit(repo, changed_files(repo, base_sha))
    code, dirty = git(repo, "status", "--porcelain")
    if code or dirty:
        raise RuntimeError("pre-commit alterou o worktree após o review")


def validate_fix(repo: Path, test_cmd: str, fixed_paths: list[str]) -> tuple[bool, str]:
    """Testa e executa pre-commit sobre os arquivos do fix antes do commit."""
    code, out = git(repo, "add", "--", *fixed_paths)
    if code:
        return False, f"git add falhou: {out[-500:]}"
    try:
        changed = changed_files(repo)
        if not changed:
            return False, "nenhuma alteração para validar"
        code, tree = git(repo, "write-tree")
        if code:
            return False, f"snapshot do fix falhou: {tree[-300:]}"
        isolated_tests(repo, tree, test_cmd)
        trusted_precommit(repo, changed)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)[-500:]
    return True, f"testes isolados e pre-commit verdes ({len(changed)} arquivo(s))"


def merge_converged_mr(
    base: str,
    pid: int,
    iid: int,
    token: str,
    reviewed_sha: str,
    repo: Path,
    target: str,
    base_sha: str,
) -> str:
    """Reconsulta o estado do MR e faz merge somente do SHA revisado."""
    path = f"/api/v4/projects/{pid}/merge_requests/{iid}"
    for _ in range(10):
        code, mr = api_request(base, path + "?with_merge_status_recheck=true", token)
        if code == 200 and mr.get("detailed_merge_status") not in (
            "unchecked",
            "checking",
            "preparing",
        ):
            break
        time.sleep(2)
    if code != 200 or mr.get("detailed_merge_status") != "mergeable":
        raise RuntimeError(
            f"MR !{iid} não está mergeable: HTTP {code}, "
            f"{mr.get('detailed_merge_status')}"
        )
    if mr.get("sha") != reviewed_sha:
        raise RuntimeError(f"MR !{iid} mudou de SHA após o review; merge cancelado")
    assert_refs_unchanged(repo, target, mr["source_branch"], base_sha, reviewed_sha)
    code, merged = api_request(
        base,
        path + "/merge",
        token,
        {"sha": mr["sha"], "should_remove_source_branch": False},
        "PUT",
    )
    if code != 200 or merged.get("state") != "merged":
        raise RuntimeError(
            f"merge do MR !{iid} falhou: HTTP {code} {str(merged)[:200]}"
        )
    return str(merged["merge_commit_sha"])


def post_finding_comments(
    base: str,
    pid: int,
    iid: int,
    tok: str,
    refs: dict[str, Any],
    findings: list[dict[str, Any]],
    cycle: int,
) -> int:
    posted = 0
    for c in findings:
        path = c.get("path")
        line = c.get("start_line") or c.get("end_line") or 1
        body = (
            f"**[CICLO {cycle} · {str(c.get('severity', 'medium')).upper()} · "
            f"{c.get('category', 'other')}]** {(c.get('content') or '').strip()}"
        )
        if c.get("suggestion_code"):
            body += f"\n\nSugestão:\n```suggestion\n{c['suggestion_code']}\n```"
        code, _ = o.bearer_request(
            f"{base}/api/v4/projects/{pid}/merge_requests/{iid}/discussions",
            method="POST",
            token=tok,
            payload={
                "body": body,
                "position": {
                    "base_sha": refs.get("base_sha"),
                    "head_sha": refs.get("head_sha"),
                    "start_sha": refs.get("start_sha"),
                    "position_type": "text",
                    "new_path": path,
                    "old_path": path,
                    "new_line": line,
                },
            },
        )
        if code not in (200, 201):
            raise RuntimeError(
                f"finding do ciclo {cycle} não foi publicado no MR !{iid}: HTTP {code}"
            )
        posted += 1
    return posted


def gate_check(path: Path) -> tuple[bool, str]:
    """Gate sintático de um arquivo .py pós-fix.

    ruff (regra F821, undefined-name) pega o erro estático clássico do fixer
    — usar um nome não importado (caso real do MR !25: `logging.exception`
    sem `import logging`); sem ruff no PATH, cai para py_compile (apenas
    sintaxe). Retorna (ok, cauda da saída).
    """
    ruff = shutil.which("ruff")
    if ruff:
        r = subprocess.run(
            [
                ruff,
                "check",
                "--isolated",
                "--select",
                "F821,E9",
                "--no-cache",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return r.returncode == 0, (r.stdout + r.stderr).strip()[-200:]
    r = subprocess.run(
        [sys.executable, "-m", "py_compile", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    return r.returncode == 0, (r.stderr or "").strip()[-200:]


def apply_fixes(
    repo: Path,
    findings: list[dict[str, Any]],
    fixer: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Fixer corrige cada finding com gate sintático por fix.

    O gate roda no arquivo após cada fix; um fix que introduza erro estático
    é revertido na hora e registrado como skip — nunca chega ao commit.
    Arquivos cujo baseline já reprova o gate não são gateados (o erro não é
    atribuível ao fixer). Retorna (aplicados, motivos de skip).
    """
    sources: dict[Path, str] = {}
    gateable: dict[Path, bool] = {}
    applied: list[dict[str, Any]] = []
    skipped: list[str] = []
    repo_resolved = repo.resolve()
    for i, c in enumerate(findings):
        rel = str(c.get("path") or "")
        path = (repo / rel).resolve() if rel else None
        if not path or not path.is_relative_to(repo_resolved) or not path.is_file():
            skipped.append(f"[{i}] caminho inválido: {rel}")
            continue
        if path not in sources:
            sources[path] = path.read_text(encoding="utf-8")
            if path.suffix == ".py":
                baseline_ok, _ = gate_check(path)
                gateable[path] = baseline_ok
            else:
                gateable[path] = False
        fix_text = two_provider.fix_finding(c, fixer)
        candidate = apply_patch(sources[path], c, parse_code_block(fix_text))
        if candidate is None:
            skipped.append(f"[{i}] patch não aplicável ({rel})")
            continue
        if gateable.get(path):
            path.write_text(candidate, encoding="utf-8")
            ok, detail = gate_check(path)
            if not ok:
                path.write_text(sources[path], encoding="utf-8")
                skipped.append(f"[{i}] reprovado no gate sintático ({rel}): {detail}")
                continue
        sources[path] = candidate
        applied.append(c)
    for path, src in sources.items():
        if src != path.read_text(encoding="utf-8"):
            path.write_text(src, encoding="utf-8")
    return applied, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--branch", help="branch com as mudanças (cria o MR)")
    source.add_argument("--mr-iid", type=int, help="loop em MR existente")
    parser.add_argument(
        "--repo", type=Path, required=True, help="clone git com push no origin"
    )
    parser.add_argument(
        "--target", default="develop", help="branch base/target (default develop)"
    )
    parser.add_argument("--title", default=None, help="título do MR criado")
    parser.add_argument(
        "--reviewer", default="deepseek", help="provider revisor (default deepseek)"
    )
    parser.add_argument(
        "--review-model", default="deepseek-v4-pro", help="modelo revisor"
    )
    parser.add_argument(
        "--effort",
        choices=("medium", "high"),
        default="medium",
        help="profundidade do OCR (default medium)",
    )
    parser.add_argument(
        "--max-tokens-budget",
        type=int,
        default=0,
        help="teto de tokens do OCR; 0 desliga o teto",
    )
    parser.add_argument(
        "--review-timeout-seconds",
        type=int,
        default=1800,
        help="limite total do OCR em segundos (default 1800)",
    )
    parser.add_argument(
        "--fixer", default="magalu", help="provider fixer (default magalu)"
    )
    parser.add_argument(
        "--max-cycles", type=int, default=3, help="máx. de reviews (default 3)"
    )
    parser.add_argument(
        "--background", default=None, help="prompt de review customizado"
    )
    parser.add_argument(
        "--test-cmd",
        required=True,
        help="comando de testes escolhido para o repositório, executado isolado",
    )
    parser.add_argument(
        "--auto-merge", action="store_true", help="merge do MR após convergência"
    )
    args = parser.parse_args(argv)
    if args.max_cycles < 1:
        parser.error("--max-cycles deve ser >= 1")
    if args.max_tokens_budget < 0:
        parser.error("--max-tokens-budget deve ser >= 0")
    if args.review_timeout_seconds < 60:
        parser.error("--review-timeout-seconds deve ser >= 60")

    lab = o.load_lab(o.LAB_DIR)
    base = lab["url"].rstrip("/")
    pid = int(lab["project"]["id"])
    bot_token = lab["bot_token"]
    pipeline_token = o.root_pat(base, o.DEFAULT_CONTAINER, o.LAB_DIR)
    identities = bot_identities()
    reviewer_user = identities["reviewer"]["user"]
    fixer_user = identities["fixer"]["user"]
    reviewer_id = reviewer_user_id(base, bot_token, reviewer_user)
    repo = args.repo
    code, out = git(repo, "status", "--porcelain")
    if code or out:
        print("autofix_loop: clone deve começar com árvore limpa", file=sys.stderr)
        return 1
    code, out = git(repo, "fetch", "origin")
    if code:
        print(f"autofix_loop: fetch falhou: {out[:200]}", file=sys.stderr)
        return 1
    if args.branch:
        code, checked_out = git(repo, "branch", "--show-current")
        if code or checked_out != args.branch:
            print(
                f"autofix_loop: clone deve estar na branch {args.branch}",
                file=sys.stderr,
            )
            return 1

    # 1) MR: cria da branch ou usa o existente
    if args.branch:
        code, out = git(repo, "push", "origin", args.branch)
        if code != 0:
            print(f"autofix_loop: push da branch falhou: {out[:200]}", file=sys.stderr)
            return 1
        code, mr = api_request(
            base,
            f"/api/v4/projects/{pid}/merge_requests",
            bot_token,
            method="POST",
            payload={
                "source_branch": args.branch,
                "target_branch": args.target,
                "title": args.title or f"saas: loop review→fix ({args.branch})",
                "description": (
                    "MR processado pelo loop do SaaS: bot revisor comenta, fixer "
                    "corrige a partir dos comentários e o re-review verifica até "
                    "convergir. Dois providers e duas identidades OAuth G6."
                ),
                "remove_source_branch": False,
                "reviewer_ids": [reviewer_id],
            },
        )
        if code not in (200, 201):
            print(
                f"autofix_loop: MR falhou: HTTP {code} {str(mr)[:200]}", file=sys.stderr
            )
            return 1
        mr_iid = int(mr["iid"])
        branch = args.branch
        target = args.target
    else:
        code, mr = api_request(
            base, f"/api/v4/projects/{pid}/merge_requests/{args.mr_iid}", bot_token
        )
        if code != 200:
            print(
                f"autofix_loop: MR !{args.mr_iid} inacessível: HTTP {code}",
                file=sys.stderr,
            )
            return 1
        mr_iid = args.mr_iid
        branch = mr["source_branch"]
        target = mr["target_branch"]
    if not args.branch:
        code, checked_out = git(repo, "branch", "--show-current")
        if code or checked_out != branch:
            print(f"autofix_loop: clone deve estar na branch {branch}", file=sys.stderr)
            return 1
    assign_reviewer(base, pid, mr_iid, bot_token, reviewer_id)
    print(f"MR !{mr_iid} ({branch} -> {target})")

    require_trusted_ci(repo, target, branch)
    pipeline_id = trigger_pipeline(base, pid, branch, pipeline_token)
    print(f"pipeline #{pipeline_id} disparada ({branch})")

    g6 = g6_open(base, identities)
    reviewer_token = g6["tokens"]["reviewer"]
    fixer_token = g6["tokens"]["fixer"]
    fixer_model = (
        two_provider.load_providers().get(args.fixer, {}).get("model", args.fixer)
    )
    converged = False
    total_fixed = 0
    reviewed_sha = ""
    try:
        for cycle in range(1, args.max_cycles + 1):
            code, fetched = git(repo, "fetch", "origin")
            if code:
                raise RuntimeError(f"fetch antes do review falhou: {fetched[-300:]}")
            require_trusted_ci(repo, target, branch)
            code, base_sha = git(repo, "rev-parse", f"origin/{target}")
            code, branch_sha = git(repo, "rev-parse", f"origin/{branch}")
            background = args.background or DEFAULT_BACKGROUND
            if cycle > 1:
                background += REREVIEW_SUFFIX
            preview = review_preview(
                repo,
                base_sha.strip(),
                branch_sha.strip(),
                args.reviewer,
                args.review_model,
                args.effort,
                args.max_tokens_budget,
            )
            review = run_review(
                repo,
                base_sha.strip(),
                branch_sha.strip(),
                args.reviewer,
                args.review_model,
                background,
                args.effort,
                args.max_tokens_budget,
                args.review_timeout_seconds,
            )
            findings = validate_review_result(
                review,
                preview,
                base_sha.strip(),
                branch_sha.strip(),
                args.reviewer,
                args.review_model,
            )
            reviewed_sha = branch_sha.strip()
            reviewer = (
                f"{review.get('llm', {}).get('provider')}/"
                f"{review.get('llm', {}).get('model')}"
            )
            print(f"ciclo {cycle}: {len(findings)} findings (revisor {reviewer})")

            code, mr_now = api_request(
                base, f"/api/v4/projects/{pid}/merge_requests/{mr_iid}", bot_token
            )
            refs = (mr_now or {}).get("diff_refs", {}) if code == 200 else {}

            if not findings:
                assert_refs_unchanged(
                    repo, target, branch, base_sha.strip(), reviewed_sha
                )
                require_trusted_ci(repo, target, branch)
                validate_head(repo, base_sha.strip(), reviewed_sha, args.test_cmd)
                wait_pipeline(base, pid, pipeline_id, reviewed_sha, pipeline_token)
                assert_refs_unchanged(
                    repo, target, branch, base_sha.strip(), reviewed_sha
                )
                approve_review(
                    base, pid, mr_iid, reviewer_token, reviewed_sha, reviewer_user
                )
                converged = True
                post_note(
                    base,
                    pid,
                    mr_iid,
                    reviewer_token,
                    f"**✅ Loop review→comentários→fix CONVERGIDO** — verificação do "
                    f"ciclo {cycle} (revisor {reviewer}): **0 findings** · "
                    f"{total_fixed} correção(ões) aplicada(s) pelo fixer "
                    f"**{fixer_model}**. Aprovação GitLab por **{reviewer_user}** "
                    f"no SHA `{reviewed_sha[:12]}`.",
                )
                break

            if cycle == 1:
                summary = two_provider.summarize_findings(review, args.fixer).strip()
                post_note(
                    base,
                    pid,
                    mr_iid,
                    reviewer_token,
                    f"<!-- ocr-summary -->\n## 🤖 OCR Review — MR !{mr_iid}\n\n"
                    f"**{len(findings)} findings** · revisor **{reviewer}** · o fixer "
                    f"**{fixer_model}** corrige a partir destes comentários.\n\n"
                    f"{summary}\n\n"
                    "_Dois providers; reviewer e fixer usam OAuth G6 sem PAT._",
                )
            posted = post_finding_comments(
                base, pid, mr_iid, reviewer_token, refs, findings, cycle
            )
            print(f"  comentários: {posted}/{len(findings)}")

            applied, skipped = apply_fixes(repo, findings, args.fixer)
            for s in skipped:
                print(f"  skip: {s}")
            if not applied:
                post_note(
                    base,
                    pid,
                    mr_iid,
                    fixer_token,
                    f"**Ciclo {cycle}**: fixer não conseguiu aplicar correções "
                    f"({len(skipped)} skip). Findings permanecem para revisão humana.",
                )
                break
            valid, detail = validate_fix(
                repo, args.test_cmd, [str(item["path"]) for item in applied]
            )
            if not valid:
                post_note(
                    base,
                    pid,
                    mr_iid,
                    fixer_token,
                    f"**Ciclo {cycle} bloqueado antes do commit:** {detail}",
                )
                print(f"autofix_loop: {detail}", file=sys.stderr)
                return 1
            print(f"  validação: {detail}")
            code, out = git(
                repo,
                "-c",
                f"user.name={fixer_user}",
                "-c",
                f"user.email={fixer_user}@lab.local",
                "commit",
                "-m",
                f"fix(saas): fixer {fixer_model} corrige {len(applied)} findings "
                f"do ciclo {cycle} (revisor {reviewer})",
            )
            if code:
                print(f"autofix_loop: commit falhou: {out[-300:]}", file=sys.stderr)
                return 1
            code, out = git(repo, "push", "origin", branch)
            if code != 0:
                print(f"autofix_loop: push do fix falhou: {out[:200]}", file=sys.stderr)
                return 1
            require_trusted_ci(repo, target, branch)
            pipeline_id = trigger_pipeline(base, pid, branch, pipeline_token)
            code, fix_sha = git(repo, "rev-parse", "HEAD")
            total_fixed += len(applied)
            post_note(
                base,
                pid,
                mr_iid,
                fixer_token,
                f"**🔧 Ciclo {cycle}** — fixer **{fixer_model}** aplicou "
                f"{len(applied)} correção(ões) a partir dos comentários — commit "
                f"`{fix_sha.strip()[:12]}`."
                + (f" Skips: {len(skipped)}." if skipped else "")
                + f" Pipeline #{pipeline_id} disparada.",
            )
            print(f"  fix: {len(applied)} aplicados, {len(skipped)} skips")
            if skipped:
                post_note(
                    base,
                    pid,
                    mr_iid,
                    reviewer_token,
                    f"**Ciclo {cycle} não convergido:** {len(skipped)} finding(s) "
                    "não receberam fix aplicável. Revisão humana necessária.",
                )
                break
        else:
            post_note(
                base,
                pid,
                mr_iid,
                reviewer_token,
                f"**⚠️ Loop não convergiu** em {args.max_cycles} ciclos — "
                f"{total_fixed} correção(ões) aplicada(s); findings restantes "
                "precisam de revisão humana.",
            )
    finally:
        g6_close(base, g6)

    if converged and args.auto_merge:
        merge_sha = merge_converged_mr(
            base,
            pid,
            mr_iid,
            bot_token,
            reviewed_sha,
            repo,
            target,
            base_sha.strip(),
        )
        print(f"MR !{mr_iid} merged: {merge_sha}")

    status = "CONVERGIDO" if converged else "NÃO CONVERGIDO"
    print(
        f"\n{status} · {total_fixed} correções · MR !{mr_iid}: "
        f"{base}/lab/sandbox/-/merge_requests/{mr_iid}"
    )
    return 0 if converged else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
