"""Assemble results/lab-manifest.json: versions, digests, host snapshot."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

GITLAB_IMAGE = "gitlab/gitlab-ce:18.4.1-ce.0"
GITLAB_BASE_URL = "http://localhost:8929"
GITLAB_HTTP_PORT = 8929
GITLAB_SSH_PORT = 2224
LAB_DIR = Path(".lab")
OCR_DIR = Path("open-code-review")
OCR_BIN = Path("bin/ocr")
RESULTS_PATH = Path("results/lab-manifest.json")
CHUNK_SIZE = 1 << 20


def _read_text(path: Path) -> str | None:
    try:
        content = path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    return content or None


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _git_commit(repo: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _ocr_version() -> str | None:
    for command in ([str(OCR_BIN), "version"], ["ocr", "version"]):
        try:
            proc = subprocess.run(command, capture_output=True, text=True, check=False)
        except OSError:
            continue
        if proc.returncode != 0:
            continue
        for line in proc.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[0] == "open-code-review":
                return fields[1]
    return None


def _ocr_block() -> dict:
    return {
        "commit": _git_commit(OCR_DIR),
        "version": _ocr_version(),
        "install": _read_text(LAB_DIR / "ocr-install") or "build-from-source",
        "npm_fallback_reason": _read_text(LAB_DIR / "ocr-fallback-reason"),
    }


def _image_digest() -> str | None:
    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", GITLAB_IMAGE, "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _root_session_token() -> str | None:
    return _read_text(LAB_DIR / "root-token")


def _gitlab_version() -> str | None:
    token = None
    credentials = _load_json(LAB_DIR / "gitlab.json")
    if isinstance(credentials, dict):
        token = credentials.get("bot_token")
    if not token:
        token = _root_session_token()
    if not token:
        return None
    req = urllib.request.Request(
        f"{GITLAB_BASE_URL}/api/v4/version", headers={"PRIVATE-TOKEN": token}
    )
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    return body.get("version")


def _gitlab_block() -> dict:
    state = _load_json(LAB_DIR / "gitlab-state.json")
    boot_seconds = state.get("boot_seconds") if isinstance(state, dict) else None
    return {
        "image": GITLAB_IMAGE,
        "image_digest": _image_digest(),
        "http_port": GITLAB_HTTP_PORT,
        "ssh_port": GITLAB_SSH_PORT,
        "boot_seconds": boot_seconds,
        "version": _gitlab_version(),
    }


def _sha256(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _review_model_block(home: Path) -> dict:
    l1 = _load_json(LAB_DIR / "l1-report.json")
    l1_data = l1 if isinstance(l1, dict) else {}
    return {
        "commit": _git_commit(home),
        "adapters_safetensors_sha256": _sha256(
            home / "adapters" / "review" / "adapters.safetensors"
        ),
        "gold_all_jsonl_sha256": _sha256(home / "data" / "eval" / "gold.all.jsonl"),
        "mlx_lm": l1_data.get("mlx_lm"),
        "mlx": l1_data.get("mlx"),
    }


def _ram_total_gb() -> int | None:
    try:
        proc = subprocess.run(
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip()) // 2**30
    except ValueError:
        return None


def _page_count(stats: str, label: str) -> int:
    for line in stats.splitlines():
        if not line.startswith(label + ":"):
            continue
        value = line.split(":", 1)[1].strip().rstrip(".")
        if value.isdigit():
            return int(value)
    return 0


def _page_size(stats: str) -> int:
    marker = "page size of "
    if marker in stats:
        parts = stats.split(marker, 1)[1].split()
        if parts and parts[0].isdigit():
            return int(parts[0])
    try:
        return os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError):
        return 16384


def _ram_free_gb() -> float | None:
    try:
        proc = subprocess.run(["vm_stat"], capture_output=True, text=True, check=False)
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    stats = proc.stdout
    page_size = _page_size(stats)
    pages = _page_count(stats, "Pages free") + _page_count(stats, "Pages inactive")
    return round(pages * page_size / 2**30, 1)


def _collect_host_snapshot() -> dict:
    repo_root = Path(__file__).resolve().parent.parent.parent
    usage = shutil.disk_usage(repo_root)
    return {
        "ram_total_gb": _ram_total_gb(),
        "ram_free_gb": _ram_free_gb(),
        "disk_total_gb": int(usage.total / 2**30),
        "disk_free_gb": int(usage.free / 2**30),
    }


def _host_block() -> dict:
    snapshot_path = LAB_DIR / "host-snapshot.json"
    existing = _load_json(snapshot_path)
    if isinstance(existing, dict):
        return existing
    snapshot = _collect_host_snapshot()
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(
        json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return snapshot


def main() -> None:
    default_home = os.environ.get(
        "REVIEW_HOME",
        str(Path.home() / "workspace" / "qwen-coder-quality" / "review-model"),
    )
    parser = argparse.ArgumentParser(
        description="Assemble results/lab-manifest.json for the lab.",
    )
    parser.add_argument("--review-home", default=default_home)
    args = parser.parse_args()
    home = Path(args.review_home).expanduser()

    manifest = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ocr": _ocr_block(),
        "tools": _load_json(LAB_DIR / "tools-report.json"),
        "gitlab": _gitlab_block(),
        "review_model": _review_model_block(home),
        "host": _host_block(),
    }
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    gitlab_version = manifest["gitlab"]["version"] or "unreachable"
    ocr_commit = manifest["ocr"]["commit"] or "unknown"
    print(f"lab_manifest: {RESULTS_PATH} (ocr {ocr_commit}, gitlab {gitlab_version})")


if __name__ == "__main__":
    main()
