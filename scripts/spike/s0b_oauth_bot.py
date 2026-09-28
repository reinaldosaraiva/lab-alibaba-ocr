"""Spike S0-B (P001-S006), cell (b) bot seat: OAuth app + bot user dedicado.

Diferença do s0b_oauth.py (que mediu com o admin `root`): aqui o resource
owner do password grant é a conta dedicada `reinaldo.saraiva` (id 35, admin
no lab) — o "bot user" que o objetivo (b) pede ("OAuth app + bot user").
Usar conta dedicada (não o admin) é o padrão de produção: o bot autentica
como ele mesmo, e a revogação/expiração do token não afeta o admin.

O app OAuth é criado como root (admin) — quem aprova/cria apps é o admin —
mas o token é emitido para `reinaldo.saraiva`. A evidência-chave é
`user_check.username == reinaldo.saraiva` (o token autentica como o bot,
não como o admin).

Senha do bot: GITLAB_BOT_PASSWORD (env ou .lab/gitlab.env, setdefault — env
explícito vence; nunca logada). Username: GITLAB_BOT_USERNAME (default
reinaldo.saraiva).

Uso:
    python3 scripts/spike/s0b_oauth_bot.py [--url URL] [--out ARQUIVO]
        [--container gitlab-gitlab-1]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lab"))
from gitlab_bootstrap import _request
from s0b_oauth import (
    APP_SCOPES,
    DEFAULT_CONTAINER,
    LAB_DIR,
    MIN_SCOPES,
    REDIRECT_URI,
    _error_message,
    bearer_request,
    create_application,
    delete_application,
    delete_stale_applications,
    load_lab,
    load_lab_env,
    password_grant,
    refresh_grant,
    revoke_token,
    root_pat,
)
from s0b_report import fingerprint, now_iso, timed, write_evidence

APP_NAME = "s0b-oauth-bot"
DEFAULT_BOT_USERNAME = "reinaldo.saraiva"
OUT = Path("results/p001-s006-s0b/oauth-bot.json")


def _fail(message: str) -> None:
    print(f"s0b_oauth_bot: {message}", file=sys.stderr)
    raise SystemExit(1)


def _revoke_everything(
    base_url: str,
    pat: str,
    app: dict[str, Any] | None,
    live: dict[str, str],
) -> dict[str, Any]:
    """Revoga tokens vivos → deleta o app → VERIFICA (401/404 esperados)."""
    record: dict[str, Any] = {"tokens": [], "delete_application": None, "verify": {}}
    if app is None:
        return record
    for label, token in live.items():
        code = revoke_token(base_url, app, token)
        record["tokens"].append({"which": label, "http": code})
    code = delete_application(base_url, pat, int(app["id"]))
    record["delete_application"] = {"http": code}
    check, _ = _request(f"{base_url}/api/v4/applications/{app['id']}", token=pat)
    record["verify"]["get_application"] = check
    if live.get("refreshed_access"):
        code, _ = bearer_request(
            f"{base_url}/api/v4/user", token=live["refreshed_access"]
        )
        record["verify"]["access_token_rejected"] = code
    return record


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
    bot_username = os.environ.get("GITLAB_BOT_USERNAME", DEFAULT_BOT_USERNAME)
    bot_password = os.environ.get("GITLAB_BOT_PASSWORD", "")
    if not bot_password:
        _fail("GITLAB_BOT_PASSWORD ausente (env ou .lab/gitlab.env)")
    pat = root_pat(base, args.container, LAB_DIR)

    timings: dict[str, float] = {}
    result: dict[str, Any] = {
        "session": "P001-S006",
        "cell": "(b) bot seat — OAuth app + bot user dedicado",
        "generated_at": now_iso(),
        "gitlab_version": None,
        "bot_user": bot_username,
        "app": None,
        "password_grant": None,
        "refresh_grant": None,
        "scope_test": None,
        "revocation": None,
    }
    app: dict[str, Any] | None = None
    live: dict[str, str] = {}

    def abort(message: str) -> None:
        result["error"] = message
        _fail(message)

    try:
        code, body = _request(f"{base}/api/v4/version", token=pat)
        if code == 200 and isinstance(body, dict):
            result["gitlab_version"] = body.get("version")

        # confere que a conta do bot existe (evidência de que o bot user é real)
        code, bot = _request(f"{base}/api/v4/users?username={bot_username}", token=pat)
        if code != 200 or not isinstance(bot, list) or not bot:
            abort(f"conta do bot {bot_username} não encontrada (HTTP {code})")
        result["bot_user_id"] = bot[0].get("id")
        result["bot_user_state"] = bot[0].get("state")

        with timed(timings, "delete_stale_applications"):
            stale = delete_stale_applications(base, pat, APP_NAME)
        with timed(timings, "create_application"):
            app = create_application(base, pat, APP_NAME, REDIRECT_URI, APP_SCOPES)
        result["app"] = {
            "id": app.get("id"),
            "name": app.get("name") or APP_NAME,
            "application_id": app.get("application_id"),
            "redirect_uri": app.get("callback_url") or REDIRECT_URI,
            "scopes": app.get("scopes") or list(APP_SCOPES),
            "secret_sha256": fingerprint(str(app["secret"])),
            "stale_removed": stale,
        }

        # password grant para o BOT USER (não o admin root)
        with timed(timings, "password_grant"):
            code, grant = password_grant(
                base, app, bot_username, bot_password, APP_SCOPES
            )
        if code != 200 or not isinstance(grant, dict) or not grant.get("access_token"):
            abort(f"password grant falhou (HTTP {code}): {_error_message(grant)}")
        access1 = str(grant["access_token"])
        refresh1 = str(grant.get("refresh_token") or "")
        live["api_grant_access"] = access1
        if refresh1:
            live["api_grant_refresh"] = refresh1
        result["password_grant"] = {
            "scopes": list(APP_SCOPES),
            "expires_in": grant.get("expires_in"),
            "created_at": grant.get("created_at"),
            "scope_returned": grant.get("scope"),
            "access_token_sha256": fingerprint(access1),
            "refresh_token_sha256": fingerprint(refresh1) if refresh1 else None,
        }
        with timed(timings, "whoami_bearer"):
            code, me = bearer_request(f"{base}/api/v4/user", token=access1)
        result["password_grant"]["user_check"] = {
            "http": code,
            "username": me.get("username") if isinstance(me, dict) else None,
            "is_bot_user": (
                isinstance(me, dict) and me.get("username") == bot_username
            ),
        }
        if code != 200:
            abort(f"GET /user com token OAuth falhou (HTTP {code})")

        with timed(timings, "refresh_grant"):
            code, refreshed = refresh_grant(base, app, refresh1)
        if code != 200 or not isinstance(refreshed, dict):
            abort(f"refresh grant falhou (HTTP {code}): {_error_message(refreshed)}")
        access2 = str(refreshed["access_token"])
        refresh2 = str(refreshed.get("refresh_token") or "")
        live["refreshed_access"] = access2
        if refresh2:
            live["refreshed_refresh"] = refresh2
        result["refresh_grant"] = {
            "access_token_changed": access2 != access1,
            "refresh_token_rotated": bool(refresh2) and refresh2 != refresh1,
            "expires_in": refreshed.get("expires_in"),
            "access_token_sha256": fingerprint(access2),
        }

        # escopo mínimo: read_user nega commit status (403) — api é necessário
        with timed(timings, "min_scope_grant"):
            code, min_grant = password_grant(
                base, app, bot_username, bot_password, MIN_SCOPES
            )
        if code != 200 or not isinstance(min_grant, dict):
            abort(f"password grant read_user falhou (HTTP {code})")
        access3 = str(min_grant["access_token"])
        refresh3 = str(min_grant.get("refresh_token") or "")
        live["min_scope_access"] = access3
        if refresh3:
            live["min_scope_refresh"] = refresh3
        scope_test: dict[str, Any] = {"scopes": list(MIN_SCOPES)}
        with timed(timings, "min_scope_get_user"):
            code, _ = bearer_request(f"{base}/api/v4/user", token=access3)
        scope_test["get_user_status"] = code
        code, commits = _request(
            f"{base}/api/v4/projects/{pid}/repository/commits?per_page=1",
            token=pat,
        )
        sha = "0" * 40
        scope_test["commit_status_sha_source"] = "zeros"
        if code == 200 and isinstance(commits, list) and commits:
            sha = str(commits[0].get("id") or sha)
            scope_test["commit_status_sha_source"] = "repo"
        with timed(timings, "min_scope_post_status"):
            code, err = bearer_request(
                f"{base}/api/v4/projects/{pid}/statuses/{sha}",
                method="POST",
                token=access3,
                payload={"state": "success", "context": "s0b/bot-min-scope"},
            )
        scope_test["commit_status"] = {
            "status": code,
            "message": _error_message(err),
            "expected": 403,
            "passed": code == 403,
        }
        result["scope_test"] = scope_test
    finally:
        result["timings"] = timings
        try:
            with timed(timings, "revocation"):
                result["revocation"] = _revoke_everything(base, pat, app, live)
        except OSError as exc:
            result["revocation"] = {
                "tokens": [],
                "delete_application": None,
                "verify": {},
                "error": f"revogação inacessível: {exc}",
            }
        write_evidence(args.out, result)
    ttl = (result["password_grant"] or {}).get("expires_in")
    rotation = (result["refresh_grant"] or {}).get("refresh_token_rotated")
    bot_ok = (result["password_grant"] or {}).get("user_check", {}).get("is_bot_user")
    print(
        f"s0b_oauth_bot: bot={bot_username} (is_bot_user={bot_ok});"
        f" TTL {ttl}s; refresh rotacionado: {rotation};"
        f" tokens+app revogados — evidência em {args.out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
