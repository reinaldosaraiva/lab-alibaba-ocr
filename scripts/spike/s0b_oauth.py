"""Spike S0-B (P001-S006), entregável (b): OAuth app + token lifecycle no CE.

Mede na fase CE (GitLab.com fica BLOQUEADO — credenciais adiadas pelo dono):
criação de OAuth application como root, password grant (TTL observado do
access token = expires_in), refresh com rotação, escopo mínimo (read_user
negando commit status → api é necessário) e revogação completa (tokens +
app), verificada com chamada posterior (401/404 esperados).

Root: PAT via cache/mint do gitlab_bootstrap (.lab/root-token, docker
gitlab-rails runner como fallback); a senha vem de GITLAB_ROOT_PASSWORD ou
.lab/gitlab.env (setdefault — env explícito vence; nada é logado). O secret
do app vive só em memória; evidências gravam fingerprint sha256 e o texto
final passa por redact() antes de tocar disco.

Decisão de fluxo: as funções de app/grant/revogação são importáveis — o
s0b_external cria (e revoga) um app PRÓPRIO em vez de um modo --keep aqui,
porque app próprio não cruza estado entre runs e a limpeza fica
independente da ordem de execução (mais simples e mais seguro).

Uso:
    python3 scripts/spike/s0b_oauth.py [--url URL] [--out ARQUIVO]
        [--container gitlab-gitlab-1]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lab"))
from gitlab_bootstrap import _decode, _request, _root_token
from s0b_report import fingerprint, now_iso, timed, write_evidence

APP_NAME = "s0b-oauth-test"
REDIRECT_URI = "http://localhost:9999/callback"
APP_SCOPES = ("api", "read_user")
MIN_SCOPES = ("read_user",)
HTTP_TIMEOUT = 30.0
LAB_DIR = Path(".lab")
OUT = Path("results/p001-s006-s0b/oauth.json")
DEFAULT_CONTAINER = "gitlab-gitlab-1"


def _fail(message: str) -> None:
    print(f"s0b_oauth: {message}", file=sys.stderr)
    raise SystemExit(1)


def load_lab(lab_dir: Path) -> dict[str, Any]:
    """.lab/gitlab.json (gitignored) — url/projeto/bot do lab; bot_token só
    é usado em memória, nunca logado."""
    path = lab_dir / "gitlab.json"
    if not path.is_file():
        _fail(f"{path} ausente — rode make lab-up")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data.get("url"):
        _fail(f"{path} em formato inesperado")
    return data


def load_lab_env(lab_dir: Path) -> None:
    """GITLAB_ROOT_PASSWORD (e irmãs) de .lab/gitlab.env via setdefault:
    env explícito do operador vence; valores nunca são logados."""
    env_file = lab_dir / "gitlab.env"
    if not env_file.is_file():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def root_pat(base_url: str, container: str, lab_dir: Path) -> str:
    """PAT root pelo cache/mint do bootstrap (mesmo .lab/root-token)."""
    load_lab_env(lab_dir)
    return _root_token(
        base_url, "GITLAB_ROOT_PASSWORD", container, lab_dir / "gitlab.json"
    )


def _error_message(body: Any) -> str:
    """Mensagem de erro da API (message/error_description/error), truncada —
    evidência de tier/escopo; segredo não aparece em corpo de erro."""
    if isinstance(body, dict):
        for key in ("message", "error_description", "error"):
            value = body.get(key)
            if value:
                return str(value)[:160]
    return ""


def bearer_request(
    url: str,
    method: str = "GET",
    token: str | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float = HTTP_TIMEOUT,
) -> tuple[int, Any]:
    """(status, body) com Authorization: Bearer — mesmo contrato do
    _request do bootstrap (PRIVATE-TOKEN), para access tokens OAuth."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _decode(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _decode(exc.read())


def form_post(
    url: str, form: dict[str, str], timeout: float = HTTP_TIMEOUT
) -> tuple[int, Any]:
    """POST form-encoded (endpoints /oauth/* do Doorkeeper). O form carrega
    client_secret/senha — nunca é logado nem persistido."""
    data = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _decode(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _decode(exc.read())


def delete_stale_applications(base_url: str, token: str, name: str) -> list[int]:
    """Remove apps de runs anteriores com o mesmo nome — idempotência de
    re-execução (secret órfão morre com o app)."""
    status, body = _request(f"{base_url}/api/v4/applications", token=token)
    if status != 200 or not isinstance(body, list):
        return []
    removed: list[int] = []
    for entry in body:
        if (
            isinstance(entry, dict)
            and entry.get("name") == name
            and entry.get("id") is not None
        ):
            code, _ = _request(
                f"{base_url}/api/v4/applications/{entry['id']}",
                method="DELETE",
                token=token,
            )
            if code in (200, 204):
                removed.append(entry["id"])
    return removed


def create_application(
    base_url: str,
    token: str,
    name: str,
    redirect_uri: str,
    scopes: tuple[str, ...],
) -> dict[str, Any]:
    """Cria o OAuth app (confidential — password grant exige); o secret da
    resposta vive só na memória do caller."""
    status, body = _request(
        f"{base_url}/api/v4/applications",
        method="POST",
        token=token,
        payload={
            "name": name,
            "redirect_uri": redirect_uri,
            "scopes": " ".join(scopes),
            "confidential": True,
        },
    )
    if (
        status not in (200, 201)
        or not isinstance(body, dict)
        or not body.get("secret")
        or not body.get("application_id")
    ):
        _fail(f"criação do app {name} falhou (HTTP {status})")
    return body


def password_grant(
    base_url: str,
    app: dict[str, Any],
    username: str,
    password: str,
    scopes: tuple[str, ...],
) -> tuple[int, Any]:
    """Resource-owner password credentials; devolve (status, corpo) — o
    caller decide se a falha é evidência ou erro fatal."""
    return form_post(
        f"{base_url}/oauth/token",
        {
            "grant_type": "password",
            "username": username,
            "password": password,
            "scope": " ".join(scopes),
            "client_id": str(app["application_id"]),
            "client_secret": str(app["secret"]),
        },
    )


def refresh_grant(
    base_url: str, app: dict[str, Any], refresh_token: str
) -> tuple[int, Any]:
    return form_post(
        f"{base_url}/oauth/token",
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": str(app["application_id"]),
            "client_secret": str(app["secret"]),
        },
    )


def revoke_token(base_url: str, app: dict[str, Any], token: str) -> int:
    status, _ = form_post(
        f"{base_url}/oauth/revoke",
        {
            "token": token,
            "client_id": str(app["application_id"]),
            "client_secret": str(app["secret"]),
        },
    )
    return status


def delete_application(base_url: str, token: str, app_id: int) -> int:
    status, _ = _request(
        f"{base_url}/api/v4/applications/{app_id}", method="DELETE", token=token
    )
    return status


def _revoke_everything(
    base_url: str,
    pat: str,
    app: dict[str, Any] | None,
    live: dict[str, str],
) -> dict[str, Any]:
    """Revoga tokens vivos → deleta o app → VERIFICA (401/404 esperados).

    Falha de revogação vira registro: a evidência é gravada de qualquer
    jeito e a falha fica visível para o closeout tratar.
    """
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
    password = os.environ.get("GITLAB_ROOT_PASSWORD", "")
    if not password:
        _fail("GITLAB_ROOT_PASSWORD ausente (env ou .lab/gitlab.env)")
    pat = root_pat(base, args.container, LAB_DIR)

    timings: dict[str, float] = {}
    result: dict[str, Any] = {
        "session": "P001-S006",
        "generated_at": now_iso(),
        "gitlab_version": None,
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
            "expire_access_tokens_in": app.get("expire_access_tokens_in"),
            "stale_removed": stale,
        }

        with timed(timings, "password_grant"):
            code, grant = password_grant(base, app, "root", password, APP_SCOPES)
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

        with timed(timings, "min_scope_grant"):
            code, min_grant = password_grant(base, app, "root", password, MIN_SCOPES)
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
        # sha real quando existe; senão zeros (o gate de escopo responde
        # antes do recurso, mas sha real deixa a evidência incontroversa)
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
                payload={"state": "success", "context": "s0b/min-scope-probe"},
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
        # GitLab fora do ar no fim não pode custar a evidência: registra-se
        # a revogação inacessível em vez de deixar OSError substituir o fluxo
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
    print(
        f"s0b_oauth: TTL access token {ttl}s; refresh rotacionado: {rotation};"
        f" tokens+app revogados — evidência em {args.out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
