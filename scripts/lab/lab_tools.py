"""Check the pinned lab toolchain (S17 Q3); install the pip/brew tools."""

import json
import subprocess
import sys
from pathlib import Path

REPORT_PATH = Path(".lab/tools-report.json")

TOOLS = [
    {
        "name": "go",
        "pin": "1.26.0",
        "command": ["go", "version"],
        "install_channel": "brew go",
        "hard": True,
    },
    {
        "name": "semgrep",
        "pin": "1.145.0",
        "command": ["semgrep", "--version"],
        "install_channel": "pip semgrep==1.145.0",
        "install": ["python3", "-m", "pip", "install", "semgrep==1.145.0"],
        "hard": True,
    },
    {
        "name": "bandit",
        "pin": "1.7.5",
        "command": ["bandit", "--version"],
        "install_channel": "pip bandit==1.7.5",
        "install": ["python3", "-m", "pip", "install", "bandit==1.7.5"],
        "hard": True,
    },
    {
        "name": "codeql",
        "pin": "2.27.0",
        "command": ["codeql", "version"],
        "install_channel": "brew --cask codeql",
        "install": ["brew", "install", "--cask", "codeql"],
        "hard": False,
    },
    {
        "name": "node",
        "pin": "22.23.1",
        "command": ["node", "--version"],
        "install_channel": "brew node",
        "hard": True,
    },
    {
        "name": "npm",
        "pin": "10.9.8",
        "command": ["npm", "--version"],
        "install_channel": "npm",
        "hard": True,
    },
    {
        "name": "docker",
        "pin": "27.5.0",
        "command": ["docker", "version", "--format", "{{.Server.Version}}"],
        "install_channel": "Docker Desktop",
        "hard": True,
    },
    {
        "name": "docker_compose",
        "pin": "5.3.1",
        "command": ["docker", "compose", "version"],
        "install_channel": "Docker Desktop",
        "hard": True,
    },
    {
        "name": "python",
        "pin": "3.12.8",
        "command": ["python3", "--version"],
        "install_channel": "pyenv",
        "hard": True,
    },
]


def _version_tuple(version: str) -> tuple[int, ...]:
    parts = [int(part) for part in version.split(".")]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def _parse_version(text: str) -> str | None:
    """First dotted-numeric token, ignoring prefixes (go1.26.0, v22.23.1)."""
    for token in text.split():
        start = next((i for i, ch in enumerate(token) if ch.isdigit()), None)
        if start is None:
            continue
        candidate = token[start:].rstrip(".,;:)")
        if "." in candidate and all(c.isdigit() or c == "." for c in candidate):
            return candidate
    return None


def _detect(spec: dict) -> str | None:
    try:
        proc = subprocess.run(
            spec["command"], capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return _parse_version(f"{proc.stdout}{proc.stderr}")


def _install(spec: dict) -> None:
    command = spec.get("install")
    if not command:
        return
    print(f"lab_tools: installing {spec['name']} via {spec['install_channel']}")
    try:
        proc = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        print(f"lab_tools: {spec['name']} install failed: {exc}", file=sys.stderr)
        return
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout).strip().splitlines()
        detail = lines[-1] if lines else f"exit {proc.returncode}"
        print(f"lab_tools: {spec['name']} install failed: {detail}", file=sys.stderr)


def _print_table(report: dict) -> None:
    print(f"{'tool':<15} {'pinned':<10} {'actual':<10} status")
    print("-" * 47)
    for name in sorted(report):
        entry = report[name]
        actual = entry["actual"] if entry["actual"] is not None else "-"
        print(f"{name:<15} {entry['pinned']:<10} {actual:<10} {entry['status']}")


def main() -> None:
    report: dict = {}
    failures: list[str] = []
    for spec in TOOLS:
        name = spec["name"]
        pin = spec["pin"]
        actual = _detect(spec)
        below = actual is None or _version_tuple(actual) < _version_tuple(pin)
        if below and spec.get("install"):
            _install(spec)
            actual = _detect(spec)
            below = actual is None or _version_tuple(actual) < _version_tuple(pin)
        if actual is None:
            status = "absent"
        elif actual == pin:
            status = "ok"
        else:
            status = "drift"
        report[name] = {
            "pinned": pin,
            "actual": actual,
            "status": status,
            "install_channel": spec["install_channel"],
        }
        if spec["hard"] and below:
            failures.append(f"{name}: want >= {pin}, got {actual or 'absent'}")
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _print_table(report)
    if failures:
        for failure in failures:
            print(f"lab_tools: hard requirement not met: {failure}", file=sys.stderr)
        raise SystemExit(1)
    print("lab_tools: all hard requirements met")


if __name__ == "__main__":
    main()
