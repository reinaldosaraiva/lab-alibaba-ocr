"""Extrai o certificado (self-signed) do gateway Magalu para .lab/magalu-ca.pem.

O binário OCR (Go) valida TLS e não tem modo insecure; SSL_CERT_FILE aponta
para o CA do gateway nos runs magalu. O host vem de .lab/ocr-config.json
(não fica literal no shell).

Uso:
    python3 scripts/spike/fetch_gateway_cert.py
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

cfg = json.loads(Path(".lab/ocr-config.json").read_text(encoding="utf-8"))
url = cfg["custom_providers"]["magalu"]["url"]
host_port = url.split("//", 1)[1].split("/")[0]

proc = subprocess.run(
    ["openssl", "s_client", "-connect", host_port, "-showcerts"],
    input="",
    capture_output=True,
    text=True,
    timeout=30,
    check=False,
)
certs = re.findall(
    r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", proc.stdout, re.DOTALL
)
if not certs:
    raise SystemExit(f"nenhum certificado em {host_port}:\n{proc.stderr[:500]}")
out = Path(".lab/magalu-ca.pem")
out.write_text("\n".join(certs) + "\n", encoding="utf-8")
info = subprocess.run(
    ["openssl", "x509", "-in", str(out), "-noout", "-subject", "-issuer", "-dates"],
    capture_output=True,
    text=True,
    check=True,
).stdout
print(f"fetch_gateway_cert: {out} ({len(certs)} cert(s))\n{info}")
