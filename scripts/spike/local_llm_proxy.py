"""Proxy local de terminação TLS para o gateway Magalu (spike S0-C).

O binário OCR (Go) valida TLS contra o keychain do macOS e ignora
SSL_CERT_FILE em darwin; o gateway 10.251.196.27:8080 tem certificado
self-signed. Este proxy escuta em 127.0.0.1 (porta livre) e reencaminha
para o gateway com TLS não-verificado — o tráfego fica em localhost.

Uso:
    python3 scripts/spike/local_llm_proxy.py [--port 0]
    # imprime a porta alocada; OCR_LLM_URL=http://127.0.0.1:<port>/v1

Só reencaminha POST (chat completions, streaming SSE pass-through) e GET
/health. O host de destino vem de .lab/ocr-config.json (custom_providers
magalu) — não fica literal no shell.
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

cfg = json.loads(Path(".lab/ocr-config.json").read_text(encoding="utf-8"))
# base do host sem o sufixo /v1 — o path da requisição (que já traz /v1/...)
# é anexado inteiro, senão dobra para /v1/v1/...
GATEWAY = cfg["custom_providers"]["magalu"]["url"].rstrip("/").removesuffix("/v1")
CTX = ssl._create_unverified_context()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:  # silêncio no stdout
        pass

    def _forward(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        url = GATEWAY + self.path
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": self.headers.get("Content-Type", "application/json"),
                "Authorization": self.headers.get("Authorization", ""),
            },
            method=self.command,
        )
        try:
            with urllib.request.urlopen(req, timeout=600, context=CTX) as resp:
                self.send_response(resp.status)
                for key, value in resp.headers.items():
                    if key.lower() in (
                        "transfer-encoding",
                        "connection",
                        "content-encoding",
                    ):
                        continue
                    self.send_header(key, value)
                self.end_headers()
                while chunk := resp.read(65536):
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            self.send_response(exc.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except Exception as exc:  # noqa: BLE001 - proxy simples
            payload = json.dumps({"error": str(exc)}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    def do_POST(self) -> None:
        self._forward()

    def do_GET(self) -> None:
        if self.path == "/health":
            payload = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self._forward()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0, help="0 = porta livre")
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    port = server.server_address[1]
    print(f"local_llm_proxy: http://127.0.0.1:{port}/v1 -> {GATEWAY}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
