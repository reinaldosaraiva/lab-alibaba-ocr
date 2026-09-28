"""Spike S0-B (P001-S006), entregável (c): commit status + discussion inline
a partir de host externo com token OAuth (sem PAT permanente).

Decisão de fluxo: cria e revoga um app PRÓPRIO (s0b-external-test)
reutilizando as funções do s0b_oauth — app próprio não cruza estado entre
runs e a limpeza fica independente da ordem de execução (mais simples e
mais seguro que um modo --keep no app do s0b_oauth). O access token OAuth
(password grant, identidade root) autentica como `Authorization: Bearer`
TODAS as chamadas de evidência; a PAT root só existe para criar/deletar o
app — essa separação É a evidência G6 (sem PAT).

Passos: branch + commit (s0b-test.md) via API, commit status (context
s0b/external), MR → branch default, discussion position-less com o marcador
<!-- ocr-summary --> (mesmo SUMMARY_MARKER do blueprint
open-code-review/examples/gitlab_ci/post_review.py:~68), leitura de volta
(autor da nota = identidade do token OAuth) e limpeza completa: MR fechado,
branch deletada, tokens revogados, app deletado.

Uso:
    python3 scripts/spike/s0b_external.py [--url URL] [--out ARQUIVO]
        [--container gitlab-gitlab-1]
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lab"))
from gitlab_bootstrap import _request
from s0b_oauth import (
    _error_message,
    bearer_request,
    create_application,
    delete_application,
    delete_stale_applications,
    load_lab,
    load_lab_env,
    password_grant,
    revoke_token,
    root_pat,
)
from s0b_report import fingerprint, now_iso, timed, write_evidence

APP_NAME = "s0b-external-test"
REDIRECT_URI = "http://localhost:9999/callback"
APP_SCOPES = ("api",)
CONTEXT = "s0b/external"
TEST_FILE = "s0b-test.md"
# mesmo marcador do blueprint examples/gitlab_ci/post_review.py (sticky note)
SUMMARY_MARKER = "<!-- ocr-summary -->"
LAB_DIR = Path(".lab")
OUT = Path("results/p001-s006-s0b/external.json")
DEFAULT_CONTAINER = "gitlab-gitlab-1"


def _fail(message: str) -> None:
    print(f"s0b_external: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=None, help="default: url do .lab/gitlab.json")
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--container", default=DEFAULT_CONTAINER)
    args = parser.parse_args()

    lab = load_lab(LAB_DIR)
    base = (args.url or lab["url"]).rstrip("/")
    pid = int(lab["project"]["id"])
    try:
        _request(f"{base}/api/v4/version", token=str(lab["bot_token"]))
    except OSError:
        _fail(f"GitLab inacessível em {base} (make lab-up)")

    load_lab_env(LAB_DIR)
    password = os.environ.get("GITLAB_ROOT_PASSWORD", "")
    if not password:
        _fail("GITLAB_ROOT_PASSWORD ausente (env ou .lab/gitlab.env)")
    pat = root_pat(base, args.container, LAB_DIR)

    timings: dict[str, float] = {}
    steps: dict[str, Any] = {}
    result: dict[str, Any] = {
        "session": "P001-S006",
        "generated_at": now_iso(),
        "app": None,
        "steps": steps,
        "evidence_g6": None,
        "cleanup": {},
        "timings": timings,
    }
    app: dict[str, Any] | None = None
    access = ""
    refresh = ""
    mr_iid: int | None = None
    branch = ""

    def abort(message: str) -> None:
        result["error"] = message
        _fail(message)

    try:
        with timed(timings, "create_application"):
            stale = delete_stale_applications(base, pat, APP_NAME)
            app = create_application(base, pat, APP_NAME, REDIRECT_URI, APP_SCOPES)
        result["app"] = {
            "id": app.get("id"),
            "name": APP_NAME,
            "application_id": app.get("application_id"),
            "secret_sha256": fingerprint(str(app["secret"])),
            "stale_removed": stale,
        }
        with timed(timings, "password_grant"):
            code, grant = password_grant(base, app, "root", password, APP_SCOPES)
        if code != 200 or not isinstance(grant, dict) or not grant.get("access_token"):
            abort(f"password grant falhou (HTTP {code}): {_error_message(grant)}")
        access = str(grant["access_token"])
        refresh = str(grant.get("refresh_token") or "")
        result["token"] = {
            "grant": "password",
            "scopes": list(APP_SCOPES),
            "expires_in": grant.get("expires_in"),
            "access_token_sha256": fingerprint(access),
        }
        code, me = bearer_request(f"{base}/api/v4/user", token=access)
        identity = me.get("username") if code == 200 and isinstance(me, dict) else None
        if not identity:
            abort(f"whoami do token OAuth falhou (HTTP {code})")

        # (i) branch default garantido (sandbox pode estar vazio se o
        # ce_probe não rodou antes; root OAuth semeia o primeiro commit)
        with timed(timings, "ensure_default"):
            code, project = bearer_request(
                f"{base}/api/v4/projects/{pid}", token=access
            )
            default = (
                project.get("default_branch")
                if code == 200 and isinstance(project, dict)
                else None
            )
            if not default:
                code, body = bearer_request(
                    f"{base}/api/v4/projects/{pid}/repository/commits",
                    method="POST",
                    token=access,
                    payload={
                        "branch": "main",
                        "commit_message": "s0b: seed inicial (external)",
                        "actions": [
                            {
                                "action": "create",
                                "file_path": "s0b-seed.md",
                                "content": f"S0-B seed ({now_iso()}).\n",
                            }
                        ],
                    },
                )
                if code not in (200, 201):
                    abort(f"seed do branch default falhou (HTTP {code})")
                default = "main"
                steps["ensure_default"] = "main semeado pelo external"
            else:
                steps["ensure_default"] = f"default existente ({default})"

        # (ii) branch + commit via API (host externo, sem git local)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        branch = f"s0b/ext-{stamp}"
        with timed(timings, "branch_commit"):
            code, body = bearer_request(
                f"{base}/api/v4/projects/{pid}/repository/commits",
                method="POST",
                token=access,
                payload={
                    "branch": branch,
                    "start_branch": default,
                    "commit_message": "s0b: evidência de host externo (OAuth)",
                    "actions": [
                        {
                            "action": "create",
                            "file_path": TEST_FILE,
                            "content": f"S0-B (c) external probe ({stamp}).\n",
                        }
                    ],
                },
            )
        if code not in (200, 201) or not isinstance(body, dict):
            abort(f"commit em {branch} falhou (HTTP {code}): {_error_message(body)}")
        sha = str(body["id"])
        steps["branch"] = {"name": branch, "from": default}
        steps["commit"] = {"sha": sha, "file": TEST_FILE, "http": code}

        # (iii) commit status de host externo
        with timed(timings, "post_status"):
            code, body = bearer_request(
                f"{base}/api/v4/projects/{pid}/statuses/{sha}",
                method="POST",
                token=access,
                payload={
                    "state": "success",
                    "context": CONTEXT,
                    "description": "S0-B OAuth external probe",
                },
            )
        steps["status"] = {
            "http": code,
            "state": body.get("status") if isinstance(body, dict) else None,
            "context": CONTEXT,
            "created": code in (200, 201),
        }
        if code not in (200, 201):
            abort(f"commit status falhou (HTTP {code}): {_error_message(body)}")

        # (iv) MR + discussion position-less com o marcador do blueprint
        with timed(timings, "create_mr"):
            code, body = bearer_request(
                f"{base}/api/v4/projects/{pid}/merge_requests",
                method="POST",
                token=access,
                payload={
                    "source_branch": branch,
                    "target_branch": default,
                    "title": "S0-B (c): evidência de host externo (OAuth, sem PAT)",
                    "remove_source_branch": False,
                },
            )
        if code not in (200, 201) or not isinstance(body, dict):
            abort(f"criação do MR falhou (HTTP {code}): {_error_message(body)}")
        mr_iid = int(body["iid"])
        steps["mr"] = {"iid": mr_iid, "state": body.get("state"), "http": code}

        note_body = (
            f"{SUMMARY_MARKER}\n"
            "S0-B (c): nota position-less de host externo com token OAuth"
            f" (app {APP_NAME}) — sem PAT."
        )
        with timed(timings, "post_discussion"):
            code, body = bearer_request(
                f"{base}/api/v4/projects/{pid}/merge_requests/{mr_iid}/discussions",
                method="POST",
                token=access,
                payload={"body": note_body},
            )
        if code not in (200, 201) or not isinstance(body, dict):
            abort(f"discussion falhou (HTTP {code}): {_error_message(body)}")
        steps["discussion"] = {
            "id": body.get("id"),
            "http": code,
            "marker": SUMMARY_MARKER,
        }

        # (v) leitura de volta: QUEM aparece como autor
        with timed(timings, "readback"):
            code, discussions = bearer_request(
                f"{base}/api/v4/projects/{pid}/merge_requests/{mr_iid}/discussions",
                token=access,
            )
            author = None
            if code == 200 and isinstance(discussions, list):
                for entry in discussions:
                    for note in entry.get("notes") or []:
                        if SUMMARY_MARKER in str(note.get("body") or ""):
                            author = (note.get("author") or {}).get("username")
                            break
                    if author:
                        break
            code_st, statuses = bearer_request(
                f"{base}/api/v4/projects/{pid}/repository/commits/{sha}/statuses",
                token=access,
            )
            state = None
            if code_st == 200 and isinstance(statuses, list):
                for entry in statuses:
                    if entry.get("name") == CONTEXT or entry.get("context") == CONTEXT:
                        state = entry.get("status") or entry.get("state")
                        break
        steps["readback"] = {
            "discussions_http": code,
            "discussion_author": author,
            "statuses_http": code_st,
            "status_state": state,
            "status_context": CONTEXT,
        }
        result["evidence_g6"] = {
            "auth": "Bearer (OAuth2 password grant, app s0b-external-test)",
            "pat_used_in_evidence_calls": False,
            "token_identity": identity,
            "discussion_author": author,
            "note": "PAT root usada apenas para criar/deletar o app OAuth",
        }
        if not author:
            abort("readback não encontrou a discussion com o marcador")
    finally:
        # GitLab fora do ar no fim não pode custar a evidência: registra-se
        # a limpeza incompleta em vez de deixar OSError substituir o fluxo
        try:
            with timed(timings, "cleanup"):
                if mr_iid is not None:
                    code, body = bearer_request(
                        f"{base}/api/v4/projects/{pid}/merge_requests/{mr_iid}",
                        method="PUT",
                        token=access or None,
                        payload={"state_event": "closed"},
                    )
                    result["cleanup"]["mr_closed"] = (
                        code == 200
                        and isinstance(body, dict)
                        and body.get("state") == "closed"
                    )
                if branch:
                    quoted = urllib.parse.quote(branch, safe="")
                    code, _ = bearer_request(
                        f"{base}/api/v4/projects/{pid}/repository/branches/{quoted}",
                        method="DELETE",
                        token=access or None,
                    )
                    result["cleanup"]["branch_deleted"] = code in (200, 204)
                if app is not None:
                    revoked = []
                    for label, token in (("access", access), ("refresh", refresh)):
                        if token:
                            revoked.append(
                                {
                                    "which": label,
                                    "http": revoke_token(base, app, token),
                                }
                            )
                    result["cleanup"]["tokens_revoked"] = revoked
                    result["cleanup"]["app_deleted_http"] = delete_application(
                        base, pat, int(app["id"])
                    )
                    code, _ = _request(
                        f"{base}/api/v4/applications/{app['id']}", token=pat
                    )
                    result["cleanup"]["app_verify_404"] = code == 404
        except OSError as exc:
            result["cleanup"]["error"] = f"limpeza incompleta: {exc}"
        write_evidence(args.out, result)

    author = (steps.get("readback") or {}).get("discussion_author")
    print(
        f"s0b_external: status {CONTEXT} em {branch}; MR !{mr_iid} fechado;"
        f" autor @{author}; tokens+app revogados — evidência em {args.out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
