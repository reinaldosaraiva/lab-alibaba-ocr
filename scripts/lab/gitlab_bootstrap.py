"""Bootstrap the lab GitLab: wait for readiness and provision the sandbox.

Root-password mode drives the local self-managed GitLab CE (design contract
S17 Q12) by minting a root PAT through `docker exec gitlab-rails runner`;
token mode reuses the same provisioning against GitLab.com with an existing
access token (docs/lab-gitlab-com.md).
"""

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

STATE_PATH = Path(".lab/gitlab-state.json")
TOKEN_NAME = "lab-ocr"
HTTP_TIMEOUT = 30.0
PROBE_TIMEOUT = 5.0
POLL_SECONDS = 5.0
RUNNER_TIMEOUT = 300.0
# GitLab 18 PATs are digest-only (raw visible only at creation), so the runner
# always rotates lab-root (destroy, mint, print); .lab/root-token skips docker.
RUNNER_RUBY = (
    "u = User.find_by_username('root'); "
    "abort 'root auth failed' "
    "unless u && u.valid_password?(ENV['GITLAB_ROOT_PASSWORD']); "
    "u.personal_access_tokens.where(name: 'lab-root').destroy_all; "
    "t = u.personal_access_tokens.create(name: 'lab-root', scopes: [:api], "
    "expires_at: 1.year.from_now); "
    "abort 'PAT create failed: ' + t.errors.full_messages.to_s "
    "unless t.persisted?; "
    "print t.token"
)


def _fail(message: str) -> None:
    print(f"gitlab_bootstrap: {message}", file=sys.stderr)
    raise SystemExit(1)


def _request(
    url: str,
    method: str = "GET",
    token: str | None = None,
    payload: dict | None = None,
    timeout: float = HTTP_TIMEOUT,
) -> tuple[int, object]:
    """One JSON request; returns (status, decoded body). Network errors raise."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["PRIVATE-TOKEN"] = token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _decode(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _decode(exc.read())


def _decode(raw: bytes) -> object:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _answers(base: str) -> bool:
    """True when the API endpoint already responds (any HTTP status counts)."""
    try:
        _request(f"{base}/api/v4/version", timeout=PROBE_TIMEOUT)
    except OSError:
        return False
    return True


def _wait_ready(base: str, timeout: float) -> None:
    url = f"{base}/-/readiness"
    deadline = time.monotonic() + timeout
    while True:
        try:
            status, _ = _request(url)
        except OSError:
            status = None
        if status == 200:
            return
        if time.monotonic() >= deadline:
            _fail(f"GitLab not ready within {timeout:.0f}s at {url}")
        time.sleep(POLL_SECONDS)


def _root_token(base: str, env_name: str | None, container: str, out_path: Path) -> str:
    """Reuse the cached root PAT or mint one via gitlab-rails runner."""
    cache = out_path.parent / "root-token"
    if cache.is_file():
        cached = cache.read_text(encoding="utf-8").strip()
        if cached:
            status, _ = _request(f"{base}/api/v4/user", token=cached)
            if status == 200:
                return cached
    if not env_name:
        _fail("root-password auth needs --root-password-env")
    if not os.environ.get(env_name):
        _fail(f"environment variable {env_name} is not set")
    env_file = out_path.parent / "gitlab.env"
    command = [
        "docker",
        "exec",
        "--env-file",
        str(env_file),
        container,
        "gitlab-rails",
        "runner",
        RUNNER_RUBY,
    ]
    try:
        proc = subprocess.run(
            command, capture_output=True, text=True, timeout=RUNNER_TIMEOUT, check=False
        )
    except subprocess.TimeoutExpired:
        _fail(f"gitlab-rails runner timed out after {RUNNER_TIMEOUT:.0f}s")
    except OSError as exc:
        _fail(f"could not run docker exec ({exc})")
    token = proc.stdout.strip()
    if proc.returncode != 0 or not token:
        stderr_lines = proc.stderr.strip().splitlines()
        detail = stderr_lines[-1] if stderr_lines else f"exit {proc.returncode}"
        _fail(f"root token bootstrap failed: {detail}")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(token + "\n", encoding="utf-8")
    cache.chmod(0o600)
    return token


def _ensure_group(base: str, token: str, group: str) -> dict:
    path = urllib.parse.quote(group, safe="")
    status, body = _request(f"{base}/api/v4/groups/{path}", token=token)
    if status == 200 and isinstance(body, dict):
        return body
    status, body = _request(
        f"{base}/api/v4/groups",
        method="POST",
        token=token,
        payload={"name": group, "path": group},
    )
    if status in (200, 201) and isinstance(body, dict):
        return body
    _fail(f"could not ensure group {group} (HTTP {status})")


def _ensure_project(
    base: str, token: str, group_path: str, project: str, group_id: int
) -> dict:
    encoded = urllib.parse.quote(f"{group_path}/{project}", safe="")
    status, body = _request(f"{base}/api/v4/projects/{encoded}", token=token)
    if status == 200 and isinstance(body, dict):
        return body
    status, body = _request(
        f"{base}/api/v4/projects",
        method="POST",
        token=token,
        payload={
            "name": project,
            "path": project,
            "namespace_id": group_id,
            "visibility": "private",
        },
    )
    if status in (200, 201) and isinstance(body, dict):
        return body
    _fail(f"could not ensure project {group_path}/{project} (HTTP {status})")


def _ensure_bot(base: str, token: str, username: str) -> dict:
    query = urllib.parse.quote(username, safe="")
    status, body = _request(f"{base}/api/v4/users?username={query}", token=token)
    if status == 200 and isinstance(body, list) and body:
        return body[0]
    status, body = _request(
        f"{base}/api/v4/users",
        method="POST",
        token=token,
        payload={
            "username": username,
            "name": "OCR Bot",
            "email": f"{username}@lab.local",
            "password": secrets.token_hex(16),
            "skippable_onboarding": True,
        },
    )
    if status in (200, 201) and isinstance(body, dict):
        return body
    _fail(f"could not ensure bot user {username} (HTTP {status})")


def _reusable_token(base: str, out_path: Path) -> str | None:
    if not out_path.is_file():
        return None
    try:
        data = json.loads(out_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    token = data.get("bot_token")
    if not token:
        return None
    status, _ = _request(f"{base}/api/v4/user", token=token)
    return token if status == 200 else None


def _ensure_bot_token(base: str, admin_token: str, out_path: Path, bot_id: int) -> str:
    reused = _reusable_token(base, out_path)
    if reused:
        return reused
    status, body = _request(
        f"{base}/api/v4/users/{bot_id}/personal_access_tokens",
        method="POST",
        token=admin_token,
        payload={"name": TOKEN_NAME, "scopes": ["api"]},
    )
    if status in (200, 201) and isinstance(body, dict):
        token = body.get("token")
        if token:
            return token
    _fail(f"could not create {TOKEN_NAME} token for bot {bot_id} (HTTP {status})")


def _ensure_bot_membership(base: str, token: str, project_id: int, bot_id: int) -> None:
    # Private projects need explicit membership before the bot token can
    # read them; 409 means the bot is already a member.
    status, _ = _request(
        f"{base}/api/v4/projects/{project_id}/members",
        method="POST",
        token=token,
        payload={"user_id": bot_id, "access_level": 30},
    )
    if status not in (200, 201, 409):
        _fail(f"could not add bot {bot_id} to project {project_id} (HTTP {status})")


def _revoke(args: argparse.Namespace) -> None:
    base = args.url.rstrip("/")
    if not _answers(base):
        print(
            "gitlab_bootstrap: GitLab unreachable, skipping revoke "
            "(tokens are destroyed with the volumes)",
            file=sys.stderr,
        )
        return
    cache = Path(args.out).parent / "root-token"
    token = ""
    if cache.is_file():
        cached = cache.read_text(encoding="utf-8").strip()
        if cached:
            status, _ = _request(f"{base}/api/v4/user", token=cached)
            if status == 200:
                token = cached
    if not token:
        print(
            "gitlab_bootstrap: cached root token missing or invalid, "
            "skipping revoke (tokens die with the volumes)",
            file=sys.stderr,
        )
        return
    query = urllib.parse.quote(args.bot_username, safe="")
    status, users = _request(f"{base}/api/v4/users?username={query}", token=token)
    if status != 200 or not isinstance(users, list) or not users:
        print("gitlab_bootstrap: no bot user found, nothing to revoke", file=sys.stderr)
        return
    user_id = users[0]["id"]
    status, tokens = _request(
        f"{base}/api/v4/users/{user_id}/personal_access_tokens", token=token
    )
    if status != 200 or not isinstance(tokens, list) or not tokens:
        print("gitlab_bootstrap: bot user has no tokens", file=sys.stderr)
        return
    for entry in tokens:
        entry_id = entry.get("id") if isinstance(entry, dict) else None
        if entry_id is None:
            continue
        status, _ = _request(
            f"{base}/api/v4/personal_access_tokens/{entry_id}",
            method="DELETE",
            token=token,
        )
        print(f"gitlab_bootstrap: revoked {entry.get('name')} (HTTP {status})")
    status, own = _request(f"{base}/api/v4/personal_access_tokens", token=token)
    if status != 200 or not isinstance(own, list):
        return
    for entry in own:
        entry_id = entry.get("id") if isinstance(entry, dict) else None
        if entry_id is None or entry.get("name") != "lab-root":
            continue
        status, _ = _request(
            f"{base}/api/v4/personal_access_tokens/{entry_id}",
            method="DELETE",
            token=token,
        )
        print(f"gitlab_bootstrap: revoked lab-root (HTTP {status})")


def _write_json(path: Path, data: dict, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Wait for GitLab readiness and provision the lab sandbox.",
    )
    parser.add_argument("--url", required=True, help="GitLab base URL")
    parser.add_argument("--root-password-env", metavar="ENV")
    parser.add_argument("--token-env", metavar="ENV")
    parser.add_argument("--out", default=".lab/gitlab.json")
    parser.add_argument("--revoke", action="store_true")
    parser.add_argument("--group", default="lab")
    parser.add_argument("--project", default="sandbox")
    parser.add_argument("--bot-username", default="ocr-bot")
    parser.add_argument(
        "--container", default="gitlab-gitlab-1", help="GitLab container name"
    )
    parser.add_argument("--timeout", type=float, default=900.0)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.revoke:
        _revoke(args)
        return
    base = args.url.rstrip("/")
    fresh_boot = not _answers(base)
    started = time.monotonic()
    _wait_ready(base, args.timeout)
    elapsed = time.monotonic() - started
    token = os.environ.get(args.token_env or "")
    if token:
        auth = "token"
    else:
        auth = "root-password"
        token = _root_token(
            base, args.root_password_env, args.container, Path(args.out)
        )
    group = _ensure_group(base, token, args.group)
    project = _ensure_project(base, token, args.group, args.project, group["id"])
    if auth == "token":
        status, me = _request(f"{base}/api/v4/user", token=token)
        if status != 200 or not isinstance(me, dict):
            _fail("token-mode identity lookup failed")
        bot = {"id": me["id"], "username": me["username"]}
        bot_token = token
    else:
        bot = _ensure_bot(base, token, args.bot_username)
        _ensure_bot_membership(base, token, project["id"], bot["id"])
        bot_token = _ensure_bot_token(base, token, Path(args.out), bot["id"])
    _write_json(
        Path(args.out),
        {
            "url": args.url,
            "group": {"id": group["id"], "path": group["path"]},
            "project": {
                "id": project["id"],
                "path_with_namespace": project["path_with_namespace"],
            },
            "bot": {"id": bot["id"], "username": bot["username"]},
            "bot_token": bot_token,
            "auth": auth,
        },
        mode=0o600,
    )
    if fresh_boot or not STATE_PATH.exists():
        _write_json(STATE_PATH, {"boot_seconds": round(elapsed, 1)}, mode=0o600)
        if fresh_boot:
            print(f"gitlab_bootstrap: fresh boot took {elapsed:.0f}s")
        else:
            print(
                "gitlab_bootstrap: first provisioning after boot, "
                f"readiness wait {elapsed:.0f}s"
            )
    print(f"gitlab_bootstrap: wrote {args.out} ({auth} auth)")


if __name__ == "__main__":
    main()
