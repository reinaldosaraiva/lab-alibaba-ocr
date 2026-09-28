"""Teste do proxy local: sobe em porta fixa, faz 1 chat completion via ele.

Uso:
    python3 scripts/spike/proxy_test.py
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

PORT = 18099
cfg = json.loads(Path(".lab/ocr-config.json").read_text(encoding="utf-8"))
entry = cfg["custom_providers"]["magalu"]


def main() -> None:
    proc = subprocess.Popen(
        [sys.executable, "scripts/spike/local_llm_proxy.py", "--port", str(PORT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        line = proc.stdout.readline().strip()
        print("proxy:", line)
        body = json.dumps(
            {
                "model": entry["models"][0],
                "messages": [{"role": "user", "content": "diga ok"}],
                "max_tokens": 16,
            }
        ).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}/v1/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {entry['api_key']}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                doc = resp.read().decode()
                print(f"via proxy: HTTP {resp.status}")
                print(doc[:500])
        except urllib.error.HTTPError as exc:
            print(f"via proxy: HTTP {exc.code}")
            print(exc.read().decode("utf-8", "replace")[:500])
        except Exception as exc:  # noqa: BLE001 - diagnóstico
            print(f"via proxy falhou: {type(exc).__name__}: {exc}")
    finally:
        proc.terminate()
        with contextlib.suppress(Exception):  # diagnóstico best-effort
            out = proc.stdout.read()
            if out.strip():
                print("proxy log:", out.strip()[-500:])


if __name__ == "__main__":
    main()
