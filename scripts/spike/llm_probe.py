"""Sonda um endpoint OpenAI-compatible com uma chat completion mínima.

Lê url/key/modelo de .lab/ocr-config.json (custom_providers). Uso:
    python3 scripts/spike/llm_probe.py magalu
    python3 scripts/spike/llm_probe.py bailian-tokenplan
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

cfg = json.loads(Path(".lab/ocr-config.json").read_text(encoding="utf-8"))


def probe(name: str, scheme: str | None = None, insecure: bool = False) -> None:
    import ssl

    entry = cfg["custom_providers"][name]
    url = entry["url"].rstrip("/")
    if scheme:
        url = scheme + "://" + url.split("//", 1)[1]
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    ctx = ssl._create_unverified_context() if insecure else None
    body = json.dumps(
        {
            "model": entry["models"][0],
            "messages": [{"role": "user", "content": "diga ok"}],
            "max_tokens": 16,
        }
    ).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {entry['api_key']}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
            doc = json.loads(resp.read().decode("utf-8"))
            print(f"{name}: HTTP {resp.status}")
            print(json.dumps(doc, ensure_ascii=False)[:1200])
    except urllib.error.HTTPError as exc:
        print(f"{name}: HTTP {exc.code}")
        print(exc.read().decode("utf-8", "replace")[:1200])
    except Exception as exc:  # noqa: BLE001 - diagnóstico
        print(f"{name}: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--insecure"]
    name = args[0] if args else "magalu"
    scheme = args[1] if len(args) > 1 else None
    probe(name, scheme, "--insecure" in sys.argv)
