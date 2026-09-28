"""Verify the review-model MLX environment against the frozen pins (S17 Q4)."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ADAPTERS_PIN = "96bbcba00a0d61126be6071566f1ad46312616940d3470192dc072f41c3dd7c0"
GOLD_PIN = "680398d0db4ca0f802941b7017b9ebdc88227a792dcebd872d9d444193973cce"
MLX_LM_PIN = "0.31.3"
MLX_PIN = "0.32.0"
REPORT_PATH = Path(".lab/l1-report.json")
CHUNK_SIZE = 1 << 20
VERSION_CODE = (
    "from importlib.metadata import version; "
    "import mlx_lm, mlx; "
    "print(version('mlx-lm')); print(version('mlx'))"
)


def _fail(message: str) -> None:
    print(f"l1_check: {message}", file=sys.stderr)
    raise SystemExit(1)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mlx_versions(venv_python: Path) -> list[str]:
    proc = subprocess.run(
        [str(venv_python), "-c", VERSION_CODE],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        _fail(f"mlx import failed in {venv_python}: {proc.stderr.strip()}")
    versions = proc.stdout.split()
    if len(versions) != 2:
        _fail(f"unexpected version probe output: {proc.stdout!r}")
    return versions


def _server_help(venv_python: Path) -> None:
    console_script = venv_python.parent / "mlx_lm.server"
    commands = [
        [str(console_script), "--help"],
        [str(venv_python), "-m", "mlx_lm.server", "--help"],
    ]
    outcome = "not attempted"
    for command in commands:
        try:
            proc = subprocess.run(command, capture_output=True, text=True, check=False)
        except FileNotFoundError:
            outcome = f"{command[0]} not found"
            continue
        if proc.returncode == 0:
            return
        outcome = f"exited {proc.returncode}"
    _fail(f"mlx_lm.server --help failed: {outcome}")


def main() -> None:
    default_home = os.environ.get(
        "REVIEW_HOME",
        str(Path.home() / "workspace" / "qwen-coder-quality" / "review-model"),
    )
    parser = argparse.ArgumentParser(
        description="Check the review-model MLX environment against frozen pins.",
    )
    parser.add_argument("--review-home", default=default_home)
    args = parser.parse_args()
    home = Path(args.review_home).expanduser()

    venv_python = home / ".venv-train" / "bin" / "python"
    if not venv_python.is_file():
        _fail(f"missing venv python at {venv_python}")
    targets = (
        (home / "adapters" / "review" / "adapters.safetensors", ADAPTERS_PIN),
        (home / "data" / "eval" / "gold.all.jsonl", GOLD_PIN),
    )
    digests = {}
    for path, pinned in targets:
        if not path.is_file():
            _fail(f"missing {path}")
        digest = _sha256(path)
        print(f"{digest}  {path}")
        if digest != pinned:
            _fail(f"SHA-256 mismatch for {path.name}: got {digest}, want {pinned}")
        digests[path.name] = digest

    mlx_lm, mlx = _mlx_versions(venv_python)
    if mlx_lm != MLX_LM_PIN:
        _fail(f"mlx_lm {mlx_lm} != pinned {MLX_LM_PIN}")
    if mlx != MLX_PIN:
        _fail(f"mlx {mlx} != pinned {MLX_PIN}")
    _server_help(venv_python)

    report = {
        "mlx_lm": mlx_lm,
        "mlx": mlx,
        "adapters_sha256": digests["adapters.safetensors"],
        "gold_sha256": digests["gold.all.jsonl"],
        "ok": True,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("l1-check: OK")


if __name__ == "__main__":
    main()
