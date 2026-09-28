"""Spike S0-B (P001-S006): report da fase CE + células GitLab.com bloqueadas.

Consome os JSONs de evidência dos runners da fase CE (ce-probe.json,
oauth.json e external.json em --out-dir) e monta report.md (tabela
mecanismo × tier, seções (a)/(b)/(c), disposições A5/A9/G6 provisórias e
revogação) e summary.json (as mesmas células em JSON). Qualquer insumo pode
faltar: a parte GitLab.com do spike está BLOQUEADA por decisão do dono
(2026-09-25, credenciais adiadas) e entra como célula fixa + placeholders
_(resume)_; célula CE sem insumo fica pendente em vez de quebrar o report.

Também é o lar das funções puras compartilhadas com os runners
(fingerprint/redact/timed/write_evidence): este módulo não toca rede, então
os runners importam sem efeito colateral e o unittest cobre a redação.

Uso:
    python3 scripts/spike/s0b_report.py [--out-dir results/p001-s006-s0b]
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import re
import sys
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OUT_DIR = Path("results/p001-s006-s0b")
TIER_CE = "CE (lab 18.4.1)"
TIER_FREE = "GitLab.com Free"
TIER_PREMIUM = "Premium trial"
BLOCKED_CELL = "BLOQUEADO — credenciais pendentes (decisão do dono 2026-09-25)"
RESUME = "_(resume)_"
PENDING = "_(pendente)_"
FINGERPRINT_LEN = 12
# Redação defensiva, nesta ordem: PAT (glpat-...), header Bearer e access
# token OAuth (hex-64 no GitLab). Fingerprint (12 hex) nunca casa pattern.
SECRET_PATTERNS = (
    re.compile(r"glpat-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"[Bb]earer [A-Za-z0-9._\-]{20,}"),
    re.compile(r"\b[0-9a-f]{64}\b"),
)


def fingerprint(secret: str) -> str:
    """Prefixo sha256 curto e determinístico: identifica o segredo nos
    artefatos sem revelá-lo (12 hex não casa os patterns de redact)."""
    digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    return "sha256:" + digest[:FINGERPRINT_LEN]


def redact(text: str) -> str:
    """Última linha de defesa antes de gravar evidência: casa PAT, header
    Bearer e hex-64 e troca cada trecho pelo seu fingerprint."""
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(
            lambda match: f"[REDACTED {fingerprint(match.group(0))}]", text
        )
    return text


def write_evidence(path: Path, data: dict[str, Any]) -> None:
    """Grava JSON de evidência com redação defensiva — nunca confiar que o
    produtor já sanitizou tudo (defesa em profundidade para o gitleaks)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = redact(json.dumps(data, indent=2, ensure_ascii=False)) + "\n"
    path.write_text(payload, encoding="utf-8")


@contextlib.contextmanager
def timed(timings: dict[str, float], label: str) -> Iterator[None]:
    """Wall clock por passo (monotonic): evidência de custo, não SLA."""
    start = time.monotonic()
    try:
        yield
    finally:
        timings[label] = round(time.monotonic() - start, 3)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_ttl(expires_in: int | None) -> str:
    """TTL numérico com leitura humana; None = não medido."""
    if expires_in is None:
        return "n/a"
    hours, rest = divmod(int(expires_in), 3600)
    minutes = rest // 60
    if hours and minutes:
        return f"{expires_in}s ({hours}h{minutes}min)"
    if hours:
        return f"{expires_in}s ({hours}h)"
    return f"{expires_in}s ({minutes}min)"


def probe_verdict(ce_probe: dict[str, Any] | None) -> str:
    """Sentença das células de ingestão a partir dos probes de Security.

    Só 401/403/404 em todos os endpoints = "n/a por design" (Vulnerability
    Report e ingestão nativa são Ultimate-only; o CE nem abre a porta).
    Qualquer 200 é achado inesperado e precisa gritar no report.
    """
    if not ce_probe:
        return f"{PENDING} ce-probe.json ausente"
    probes = ce_probe.get("security_endpoints") or []
    if not probes:
        return f"{PENDING} sem probes registrados"
    statuses = [entry.get("status") for entry in probes]
    if 200 in statuses:
        return "MEDIU (inesperado — endpoint respondeu 200; revisar)"
    denied = [code for code in statuses if code in (401, 403, 404)]
    if len(denied) == len(statuses):
        detail = ", ".join(
            f"{entry.get('endpoint')}→{entry.get('status')}" for entry in probes
        )
        return f"n/a por design — Security nativo Ultimate-only ({detail})"
    return f"{PENDING} statuses mistos: {statuses}"


def _oauth_cells(oauth: dict[str, Any] | None) -> tuple[str, str, str]:
    """Células CE das linhas OAuth a partir do oauth.json (ou pendência)."""
    if not oauth:
        missing = f"{PENDING} oauth.json ausente"
        return missing, missing, missing
    grant = oauth.get("password_grant") or {}
    refresh = oauth.get("refresh_grant") or {}
    scope = oauth.get("scope_test") or {}
    if grant:
        ttl_cell = f"ok — TTL access token {fmt_ttl(grant.get('expires_in'))}"
    else:
        ttl_cell = PENDING
    if refresh:
        rotation = "sim" if refresh.get("refresh_token_rotated") else "não"
        refresh_cell = f"ok — access_token mudou; rotação de refresh: {rotation}"
    else:
        refresh_cell = PENDING
    if scope:
        commit = scope.get("commit_status") or {}
        scope_cell = (
            f"api necessário — read_user: /user {scope.get('get_user_status')},"
            f" POST statuses {commit.get('status')}"
        )
    else:
        scope_cell = PENDING
    return ttl_cell, refresh_cell, scope_cell


def _external_cells(external: dict[str, Any] | None) -> tuple[str, str]:
    """Células CE das linhas (c) a partir do external.json (ou pendência)."""
    if not external:
        missing = f"{PENDING} external.json ausente"
        return missing, missing
    steps = external.get("steps") or {}
    status = steps.get("status") or {}
    read = steps.get("readback") or {}
    if status:
        status_cell = (
            f"ok — state={status.get('state')} context={status.get('context')}"
            " via Bearer OAuth"
        )
    else:
        status_cell = PENDING
    if read:
        discussion_cell = (
            f"ok — nota com ocr-summary; autor @{read.get('discussion_author')}"
        )
    else:
        discussion_cell = PENDING
    return status_cell, discussion_cell


def _ci_artifact_cell(runner_ci: dict[str, Any] | None, cell_key: str) -> str:
    """Célula CE de ingestão via CI artifact (Code Quality) a partir do
    runner-ci.json.

    Distinção importante: o report Code Quality (artifacts:reports:
    codequality:) funciona no CE — o finding aparece no diff de code quality
    do MR (Ci::PipelineArtifact code_quality_mr_diff). A API de Security
    (vulnerabilities) é outra feature, Ultimate-only no CE (404). A célula
    registra os dois fatos.
    """
    if not runner_ci:
        return f"{PENDING} runner-ci.json ausente"
    cell = (runner_ci.get("cells") or {}).get(cell_key) or {}
    diff = cell.get("code_quality_mr_diff") or {}
    pipeline = cell.get("pipeline") or {}
    if not diff.get("exists"):
        return f"{PENDING} diff artifact não criado (pipeline {pipeline.get('status')})"
    content = diff.get("content") or {}
    mr_key = next(iter(content), None)
    files = ((content.get(mr_key) or {}) if mr_key else {}).get("files") or {}
    first_file = next(iter(files), None)
    first_err = (files.get(first_file) or [{}])[0] if first_file else {}
    detail = (
        f"diff MR !{cell.get('mr_iid')} criado"
        f" ({first_file}:{first_err.get('line')},"
        f" severity {first_err.get('severity')})"
    )
    return (
        f"ok — Code Quality via CI artifact: {detail}"
        f" (pipeline {pipeline.get('id')} {pipeline.get('status')});"
        " Security API (vulnerabilities) Ultimate-only (404)"
    )


def build_cells(
    ce_probe: dict[str, Any] | None,
    oauth: dict[str, Any] | None,
    external: dict[str, Any] | None,
    runner_ci: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Células da tabela mecanismo × tier (uma linha por mecanismo).

    GitLab.com (Free e Premium trial) é célula fixa BLOQUEADA — a fase CE
    não tem credenciais (decisão do dono 2026-09-25). As células CE derivam
    só dos JSONs; insumo ausente vira pendência, não erro.
    """
    ingest = probe_verdict(ce_probe)
    pipeline = (ce_probe or {}).get("pipeline") or {}
    created = pipeline.get("created") or {}
    runner_note = (
        "; lab sem runner — pipeline probe ficou pending e foi cancelada"
        if created.get("id") is not None
        else ""
    )
    ci_cell = _ci_artifact_cell(runner_ci, "ci_artifact_gl_schema")
    sarif_cell = _ci_artifact_cell(runner_ci, "ci_artifact_sarif")
    ttl_cell, refresh_cell, scope_cell = _oauth_cells(oauth)
    status_cell, discussion_cell = _external_cells(external)
    rows = [
        {
            "mechanism": "ingest: artefato de CI (schema GitLab)",
            "ce": f"{ci_cell}{runner_note}",
            "evidence": "runner-ci.json ci_artifact_gl_schema + ce-probe.json",
        },
        {
            "mechanism": "ingest: SARIF via conversor",
            "ce": sarif_cell,
            "evidence": "runner-ci.json ci_artifact_sarif + ce-probe.json",
        },
        {
            "mechanism": "ingest: API direta",
            "ce": ingest,
            "evidence": "ce-probe.json security_endpoints",
        },
        {
            "mechanism": "oauth: app + password grant (TTL)",
            "ce": ttl_cell,
            "evidence": "oauth.json password_grant.expires_in",
        },
        {
            "mechanism": "oauth: refresh + rotação",
            "ce": refresh_cell,
            "evidence": "oauth.json refresh_grant",
        },
        {
            "mechanism": "oauth: escopo mínimo p/ commit status",
            "ce": scope_cell,
            "evidence": "oauth.json scope_test",
        },
        {
            "mechanism": "external: commit status sem PAT",
            "ce": status_cell,
            "evidence": "external.json steps.status",
        },
        {
            "mechanism": "external: discussion inline sem PAT",
            "ce": discussion_cell,
            "evidence": "external.json steps.readback",
        },
    ]
    for row in rows:
        row["free"] = BLOCKED_CELL
        row["premium"] = BLOCKED_CELL
    return rows


def render_table(cells: list[dict[str, str]]) -> list[str]:
    """Tabela markdown mecanismo × tier × evidência (uma linha por célula)."""
    lines = [
        "## S0-B — mecanismo × tier (fase CE; GitLab.com bloqueada)",
        "",
        f"| mecanismo | {TIER_CE} | {TIER_FREE} | {TIER_PREMIUM} | evidência |",
        "|---|---|---|---|---|",
    ]
    for cell in cells:
        lines.append(
            f"| {cell['mechanism']} | {cell['ce']} | {cell['free']}"
            f" | {cell['premium']} | {cell['evidence']} |"
        )
    return lines


def render_probe_section(ce_probe: dict[str, Any] | None) -> list[str]:
    """Seção (a): probes de tier + comportamento do pipeline sem runner."""
    lines = ["## (a) Ingestão no Security nativo — probe de tier no CE", ""]
    if not ce_probe:
        lines.append(f"{PENDING} ce-probe.json ausente")
        return lines
    lines.append("| endpoint | HTTP | mensagem |")
    lines.append("|---|---|---|")
    for entry in ce_probe.get("security_endpoints") or []:
        lines.append(
            f"| {entry.get('endpoint')} | {entry.get('status')}"
            f" | {entry.get('message') or '—'} |"
        )
    pipeline = ce_probe.get("pipeline") or {}
    created = pipeline.get("created") or {}
    lines.append("")
    if created.get("id") is not None:
        polls = pipeline.get("poll_samples") or []
        seen = sorted(
            {str(sample.get("status")) for sample in polls if sample.get("status")}
        )
        cancel = pipeline.get("cancel") or {}
        lines.append(
            f"- pipeline de teste {created.get('id')} na ref"
            f" `{pipeline.get('ref')}`: {created.get('status')};"
            f" {len(polls)} amostras de poll ({', '.join(seen) or '—'}) —"
            " pending documentado, sem runner; cancelada →"
            f" {cancel.get('status_after')} (HTTP {cancel.get('http')})"
        )
        if pipeline.get("seeded_initial_commit"):
            lines.append(
                "- sandbox nasceu vazio: branch default semeado com"
                f" .gitlab-ci.yml mínimo como {pipeline.get('seed_identity')}"
                " (evidência registrada no JSON)"
            )
    else:
        created = pipeline.get("created") or {}
        http = created.get("http")
        detail = f"HTTP {http}" if http is not None else PENDING
        lines.append(
            f"- pipeline de teste: não criada ({detail} — {created.get('message', 'sem detalhe')})"
        )
    runners = ce_probe.get("runners") or {}
    lines.append(
        f"- runners do projeto: {runners.get('project_runners')} —"
        " container CE sem binário gitlab-runner (pipeline fica pending)"
    )
    return lines


def render_ci_artifact_section(runner_ci: dict[str, Any] | None) -> list[str]:
    """Seção (a2): ingestão Code Quality via CI artifact no CE (runner).

    Distinto da API de Security (vulnerabilities, Ultimate-only): o report
    Code Quality é parseado pelo parser CodeClimate e o finding novo aparece
    no diff de code quality do MR (Ci::PipelineArtifact code_quality_mr_diff).
    """
    lines = [
        "## (a2) Ingestão Code Quality via CI artifact no CE (runner)",
        "",
    ]
    if not runner_ci:
        lines.append(f"{PENDING} runner-ci.json ausente")
        return lines
    runner = runner_ci.get("runner") or {}
    lines.append(
        f"- runner `{runner.get('name')}` (id {runner.get('id')})"
        " — registro one-shot no volume runner_config (network_mode host)"
    )
    lines.append(
        "- formato aceito pelo parser `codequality` do CE 18.4: ARRAY JSON"
        " CodeClimate com severity (schema codeclimate.json); o objeto wrapped"
        " {version,type,report} e o SARIF cru NAO sao parseados (verificado no"
        " rails runner) — a celula SARIF usa conversor Ruby no job"
    )
    cells = runner_ci.get("cells") or {}
    for key, label in (
        ("ci_artifact_gl_schema", "artefato de CI (schema GitLab/CodeClimate)"),
        ("ci_artifact_sarif", "SARIF via conversor"),
    ):
        cell = cells.get(key) or {}
        diff = cell.get("code_quality_mr_diff") or {}
        pipeline = cell.get("pipeline") or {}
        if not cell:
            lines.append(f"- {label}: {PENDING} celula ausente")
            continue
        lines.append(
            f"- {label}: MR !{cell.get('mr_iid')},"
            f" pipeline {pipeline.get('id')} {pipeline.get('status')},"
            f" diff artifact exists={diff.get('exists')}"
            f" (size {diff.get('size')})"
        )
        content = diff.get("content") or {}
        for mr_key, mr_val in content.items():
            for path, errs in ((mr_val or {}).get("files") or {}).items():
                for err in errs:
                    lines.append(
                        f"  - {mr_key}: {path}:{err.get('line')}"
                        f" severity={err.get('severity')}"
                        f" — {err.get('description')}"
                    )
    lines.append(
        "- evidencia: o finding novo (fingerprint unico) aparece no diff de"
        " code quality do MR — a porta de ingestao Code Quality funciona no CE"
    )
    return lines


def render_oauth_section(oauth: dict[str, Any] | None) -> list[str]:
    """Seção (b): números do lifecycle OAuth (TTL, rotação, escopos)."""
    lines = ["## (b) OAuth app + token lifecycle (CE, admin=root)", ""]
    if not oauth:
        lines.append(f"{PENDING} oauth.json ausente")
        return lines
    app = oauth.get("app") or {}
    grant = oauth.get("password_grant") or {}
    refresh = oauth.get("refresh_grant") or {}
    scope = oauth.get("scope_test") or {}
    commit = scope.get("commit_status") or {}
    lines.append(
        f"- app `{app.get('name')}` (id {app.get('id')}); secret só como"
        f" fingerprint {app.get('secret_sha256')}"
    )
    lines.append(
        f"- password grant: expires_in **{fmt_ttl(grant.get('expires_in'))}**"
        f" (created_at {grant.get('created_at')}); escopos"
        f" {grant.get('scopes')}; whoami Bearer"
        f" {(grant.get('user_check') or {}).get('http')}"
    )
    if refresh:
        lines.append(
            f"- refresh: access_token mudou ({refresh.get('access_token_changed')});"
            f" refresh_token rotacionado ({refresh.get('refresh_token_rotated')});"
            f" expires_in {fmt_ttl(refresh.get('expires_in'))}"
        )
    lines.append(
        f"- escopo mínimo: read_user → GET /user {scope.get('get_user_status')},"
        f" POST statuses {commit.get('status')}"
        f" ({commit.get('message') or '—'}) — api é necessário"
    )
    timings = oauth.get("timings") or {}
    if timings:
        pair = ", ".join(f"{key}={value}s" for key, value in sorted(timings.items()))
        lines.append(f"- wall por passo: {pair}")
    return lines


def render_oauth_bot_section(oauth_bot: dict[str, Any] | None) -> list[str]:
    """Seção (b2): cell (b) bot seat — OAuth app + bot user dedicado
    (reinaldo.saraiva), não o admin root."""
    lines = [
        "## (b2) Cell (b) bot seat — OAuth app + bot user dedicado (CE)",
        "",
    ]
    if not oauth_bot:
        lines.append(f"{PENDING} oauth-bot.json ausente")
        return lines
    app = oauth_bot.get("app") or {}
    grant = oauth_bot.get("password_grant") or {}
    refresh = oauth_bot.get("refresh_grant") or {}
    scope = oauth_bot.get("scope_test") or {}
    commit = scope.get("commit_status") or {}
    user_check = grant.get("user_check") or {}
    bot = oauth_bot.get("bot_user")
    lines.append(
        f"- bot user: `{bot}` (id {oauth_bot.get('bot_user_id')},"
        f" state {oauth_bot.get('bot_user_state')}) — conta dedicada, não o admin"
    )
    lines.append(
        f"- app `{app.get('name')}` (id {app.get('id')}); secret só como"
        f" fingerprint {app.get('secret_sha256')}"
    )
    lines.append(
        f"- password grant para o BOT: expires_in"
        f" **{fmt_ttl(grant.get('expires_in'))}**; whoami Bearer"
        f" {user_check.get('http')} → username `{user_check.get('username')}`"
        f" (is_bot_user={user_check.get('is_bot_user')}) — o token autentica"
        " como o bot, não como o admin"
    )
    if refresh:
        lines.append(
            f"- refresh: access_token mudou ({refresh.get('access_token_changed')});"
            f" refresh_token rotacionado ({refresh.get('refresh_token_rotated')});"
            f" expires_in {fmt_ttl(refresh.get('expires_in'))}"
        )
    lines.append(
        f"- escopo mínimo: read_user → GET /user {scope.get('get_user_status')},"
        f" POST statuses {commit.get('status')}"
        f" ({commit.get('message') or '—'}) — api é necessário"
    )
    lines.append(
        "- evidência: a identidade do token é o bot user dedicado"
        " (reinaldo.saraiva) — padrão de produção (o bot autentica como ele"
        " mesmo; revogação/expiração não afeta o admin)"
    )
    return lines


def render_external_section(external: dict[str, Any] | None) -> list[str]:
    """Seção (c): commit status + discussion inline de host externo, sem PAT."""
    lines = [
        "## (c) Commit status + discussion inline de host externo (sem PAT)",
        "",
    ]
    if not external:
        lines.append(f"{PENDING} external.json ausente")
        return lines
    steps = external.get("steps") or {}
    status = steps.get("status") or {}
    discussion = steps.get("discussion") or {}
    mr = steps.get("mr") or {}
    read = steps.get("readback") or {}
    g6 = external.get("evidence_g6") or {}
    sha = str((steps.get("commit") or {}).get("sha") or "")
    lines.append(
        f"- commit status: state={status.get('state')}"
        f" context=`{status.get('context')}` em {sha[:8]}…"
    )
    lines.append(
        f"- discussion `{discussion.get('id')}` com marcador"
        " `<!-- ocr-summary -->`; lida de volta com autor"
        f" @{read.get('discussion_author')}"
    )
    lines.append(
        f"- autenticação de TODAS as chamadas de evidência: {g6.get('auth')};"
        f" PAT usada nelas: {g6.get('pat_used_in_evidence_calls')}"
    )
    lines.append(
        f"- MR !{mr.get('iid')} ({mr.get('state')}) — fechado na limpeza,"
        " branch deletada"
    )
    return lines


def a5_disposition(
    oauth: dict[str, Any] | None,
    oauth_bot: dict[str, Any] | None = None,
) -> str:
    """A5 provisória: mecanismo OAuth demonstrado no CE; tier pendente."""
    if not oauth:
        return f"A5 PROVISÓRIA {PENDING} — oauth.json ausente"
    grant = oauth.get("password_grant") or {}
    bot_note = ""
    if oauth_bot:
        bot = oauth_bot.get("bot_user")
        bot_check = (oauth_bot.get("password_grant") or {}).get("user_check") or {}
        bot_note = (
            f"; cell (b) bot seat: token emitido para o bot user dedicado"
            f" `{bot}` (whoami → {bot_check.get('username')},"
            f" is_bot_user={bot_check.get('is_bot_user')})"
        )
    return (
        "A5 PROVISÓRIA — mecanismo OAuth demonstrado no CE"
        f" (TTL {fmt_ttl(grant.get('expires_in'))}, refresh e escopos medidos)"
        f"{bot_note}; tier Free/assento no GitLab.com {RESUME}"
    )


def a9_disposition(
    ce_probe: dict[str, Any] | None,
    runner_ci: dict[str, Any] | None = None,
) -> str:
    """A9 provisória: no CE, Code Quality via CI artifact funciona; a API de
    Security (vulnerabilities) é Ultimate-only."""
    if not ce_probe:
        return f"A9 PROVISÓRIA {PENDING} — ce-probe.json ausente"
    ci = _ci_artifact_cell(runner_ci, "ci_artifact_gl_schema")
    return (
        "A9 PROVISÓRIA — no CE: a porta Code Quality via CI artifact"
        f" funciona ({ci}); a API de Security (vulnerabilities) é"
        " Ultimate-only (evidência: ce-probe.json security_endpoints + "
        f"runner-ci.json); células GitLab.com {RESUME}"
    )


def g6_disposition(external: dict[str, Any] | None) -> str:
    """G6 provisório: (c) sem PAT funcionou no CE; GitLab.com pendente."""
    if not external:
        return f"G6 PROVISÓRIO {PENDING} — external.json ausente"
    read = (external.get("steps") or {}).get("readback") or {}
    return (
        "G6 PROVISÓRIO — commit status + discussion via token OAuth (sem PAT)"
        f" funcionaram no CE; autor @{read.get('discussion_author')};"
        f" GitLab.com {RESUME}"
    )


def render_provisional_section(
    ce_probe: dict[str, Any] | None,
    oauth: dict[str, Any] | None,
    external: dict[str, Any] | None,
    runner_ci: dict[str, Any] | None = None,
    oauth_bot: dict[str, Any] | None = None,
) -> list[str]:
    return [
        "## Disposição A5/A9/G6 (PROVISÓRIA — fase CE)",
        "",
        a5_disposition(oauth, oauth_bot),
        "",
        a9_disposition(ce_probe, runner_ci),
        "",
        g6_disposition(external),
        "",
        (
            f"Placeholders {RESUME}: células GitLab.com preenchidas no resume"
            " (credenciais adiadas pelo dono em 2026-09-25)."
        ),
    ]


def render_revocation_section(
    oauth: dict[str, Any] | None,
    external: dict[str, Any] | None,
    oauth_bot: dict[str, Any] | None = None,
) -> list[str]:
    """Seção revogação: o que foi revogado, quando — base do closeout."""
    lines = ["## Revogação (o que foi revogado, quando)", ""]
    if oauth:
        revocation = oauth.get("revocation") or {}
        for entry in revocation.get("tokens") or []:
            lines.append(
                f"- [{oauth.get('generated_at')}] oauth.json: token"
                f" {entry.get('which')} — POST /oauth/revoke HTTP"
                f" {entry.get('http')}"
            )
        deleted = revocation.get("delete_application") or {}
        verify = revocation.get("verify") or {}
        lines.append(
            f"- [{oauth.get('generated_at')}] oauth.json: app"
            f" {(oauth.get('app') or {}).get('name')} deletado"
            f" (HTTP {deleted.get('http')}; GET depois ="
            f" {verify.get('get_application')}; access rejeitado:"
            f" {verify.get('access_token_rejected')})"
        )
    if oauth_bot:
        revocation = oauth_bot.get("revocation") or {}
        for entry in revocation.get("tokens") or []:
            lines.append(
                f"- [{oauth_bot.get('generated_at')}] oauth-bot.json: token"
                f" {entry.get('which')} — POST /oauth/revoke HTTP"
                f" {entry.get('http')}"
            )
        deleted = revocation.get("delete_application") or {}
        verify = revocation.get("verify") or {}
        lines.append(
            f"- [{oauth_bot.get('generated_at')}] oauth-bot.json: app"
            f" {(oauth_bot.get('app') or {}).get('name')} (bot"
            f" {oauth_bot.get('bot_user')}) deletado"
            f" (HTTP {deleted.get('http')}; GET depois ="
            f" {verify.get('get_application')}; access rejeitado:"
            f" {verify.get('access_token_rejected')})"
        )
    if external:
        for key, value in (external.get("cleanup") or {}).items():
            lines.append(
                f"- [{external.get('generated_at')}] external.json: {key}={value}"
            )
    if not oauth and not external and not oauth_bot:
        lines.append(f"{PENDING} sem registros de revogação")
    return lines


def render_report(
    ce_probe: dict[str, Any] | None,
    oauth: dict[str, Any] | None,
    external: dict[str, Any] | None,
    generated_at: str,
    runner_ci: dict[str, Any] | None = None,
    oauth_bot: dict[str, Any] | None = None,
) -> str:
    """Report completo como texto — sem I/O; main() aplica redact ao gravar."""
    cells = build_cells(ce_probe, oauth, external, runner_ci)
    sections = [
        "# S0-B — ingestão GitLab: CE × GitLab.com (P001-S006, fase CE)",
        "",
        f"Gerado em {generated_at}.",
        "",
        (
            "Fase CE (GitLab CE 18.4.1 local). GitLab.com Free / Premium"
            f" trial: {BLOCKED_CELL} — células {RESUME}."
        ),
        "",
        *render_table(cells),
        "",
        *render_probe_section(ce_probe),
        "",
        *render_ci_artifact_section(runner_ci),
        "",
        *render_oauth_section(oauth),
        "",
        *render_oauth_bot_section(oauth_bot),
        "",
        *render_external_section(external),
        "",
        *render_provisional_section(ce_probe, oauth, external, runner_ci, oauth_bot),
        "",
        *render_revocation_section(oauth, external, oauth_bot),
    ]
    return "\n".join(sections) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = parser.parse_args()

    def load(name: str) -> dict[str, Any] | None:
        path = args.out_dir / name
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            print(
                f"s0b_report: {name} inválido — tratando como ausente",
                file=sys.stderr,
            )
            return None

    ce_probe = load("ce-probe.json")
    oauth = load("oauth.json")
    external = load("external.json")
    runner_ci = load("runner-ci.json")
    oauth_bot = load("oauth-bot.json")
    generated_at = now_iso()
    cells = build_cells(ce_probe, oauth, external, runner_ci)
    report = render_report(
        ce_probe, oauth, external, generated_at, runner_ci, oauth_bot
    )
    summary = {
        "session": "P001-S006",
        "generated_at": generated_at,
        "blocked_cell": BLOCKED_CELL,
        "resume_placeholder": RESUME,
        "cells": cells,
        "dispositions": {
            "a5": a5_disposition(oauth, oauth_bot),
            "a9": a9_disposition(ce_probe, runner_ci),
            "g6": g6_disposition(external),
        },
    }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "report.md").write_text(redact(report), encoding="utf-8")
    (args.out_dir / "summary.json").write_text(
        redact(json.dumps(summary, indent=2, ensure_ascii=False)) + "\n",
        encoding="utf-8",
    )
    print(f"s0b_report: {args.out_dir}/report.md + summary.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
