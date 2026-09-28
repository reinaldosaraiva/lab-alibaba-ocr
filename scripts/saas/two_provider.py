"""Pipeline SaaS dois providers: revisor profundo x summarizer/fixer de baixo custo.

Estágios (texto gerado pelo MODELO, não template):
- summarize_findings(review, provider): summary do MR a partir dos findings;
- fix_finding(finding, provider): patch minimalista para um finding do revisor.

Roteamento (premissa do dono, 2026-09-28): deepseek-chat revisa (fundo,
pago por token) e qwen38-27b (Magalu, zero custo) sumariza/corrige — dois
providers; ou qwen38-27b nos dois papéis para custo zero total. Providers
carregados em runtime dos configs do lab — nenhum segredo nem infra neste
arquivo:
- magalu: .lab/ocr-config.json (custom_providers.magalu) + CA .lab/magalu-ca.pem
  (o gateway tem cert próprio; o CA vira contexto SSL do request);
- deepseek: ~/.opencodereview/config.json (providers.deepseek).

Uso:
    python3 scripts/saas/two_provider.py summarize --review REVIEW.json \
        [--provider magalu] [--out OUT.md]
    python3 scripts/saas/two_provider.py fix --review REVIEW.json --index 0 \
        [--provider magalu] [--out OUT.md]
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import urllib.request
from pathlib import Path
from typing import Any

LAB_DIR = Path(".lab")
MAGALU_CFG = LAB_DIR / "ocr-config.json"
MAGALU_CA = LAB_DIR / "magalu-ca.pem"
OCR_USER_CFG = Path.home() / ".opencodereview" / "config.json"
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

SUMMARY_SYSTEM = (
    "Você escreve notas de resumo de code review para um SaaS GitLab-first. "
    "Direto, técnico, sem floreio."
)
FIX_SYSTEM = (
    "Você é o estágio de correção (fixer) de um SaaS de code review. "
    "Patches minimalistas e corretos."
)


def load_providers() -> dict[str, dict[str, Any]]:
    """Lê os providers dos configs do lab/OCR. Sem segredo neste arquivo."""
    providers: dict[str, dict[str, Any]] = {}
    if MAGALU_CFG.is_file():
        lab = json.loads(MAGALU_CFG.read_text(encoding="utf-8"))
        magalu = (lab.get("custom_providers") or {}).get("magalu") or {}
        models = magalu.get("models") or []
        if magalu.get("url") and magalu.get("api_key") and models:
            # url do config é base (ex.: https://host:port/v1) — completa o path
            url = str(magalu["url"]).rstrip("/")
            if not url.endswith("/chat/completions"):
                url += "/chat/completions"
            providers["magalu"] = {
                "url": url,
                "model": models[0],
                "api_key": magalu["api_key"],
                "ca": str(MAGALU_CA.resolve()) if MAGALU_CA.is_file() else None,
            }
    if OCR_USER_CFG.is_file():
        ocr = json.loads(OCR_USER_CFG.read_text(encoding="utf-8"))
        deepseek = (ocr.get("providers") or {}).get("deepseek") or {}
        if deepseek.get("api_key"):
            providers["deepseek"] = {
                "url": DEEPSEEK_URL,
                "model": deepseek.get("model", "deepseek-chat"),
                "api_key": deepseek["api_key"],
                "ca": None,
            }
    return providers


def chat(
    provider: str,
    messages: list[dict[str, str]],
    max_tokens: int = 1600,
    timeout: int = 240,
) -> str:
    """Uma chamada chat-completions no provider (CA próprio se configurado)."""
    cfg = load_providers().get(provider)
    if not cfg:
        print(
            f"two_provider: provider {provider!r} sem config "
            f"(magalu: {MAGALU_CFG}; deepseek: {OCR_USER_CFG})",
            file=sys.stderr,
        )
        raise SystemExit(1)
    body = json.dumps(
        {
            "model": cfg["model"],
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.2,
        }
    ).encode()
    req = urllib.request.Request(
        cfg["url"],
        data=body,
        headers={
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json",
        },
    )
    if cfg.get("ca"):
        ctx = ssl.create_default_context(cafile=cfg["ca"])
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            out = json.loads(resp.read().decode())
    else:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out = json.loads(resp.read().decode())
    return out["choices"][0]["message"]["content"]


def _compact_findings(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "sev": c.get("severity"),
            "cat": c.get("category"),
            "path": c.get("path"),
            "line": c.get("start_line"),
            "finding": (c.get("content") or "").strip()[:220],
        }
        for c in comments
    ]


def summarize_findings(review: dict[str, Any], provider: str) -> str:
    """O MODELO escreve o summary do MR a partir dos findings do review."""
    comments = review.get("comments") or []
    reviewer = (
        f"{review.get('llm', {}).get('provider')}/{review.get('llm', {}).get('model')}"
    )
    user = (
        "Escreva a nota de resumo (summary) de code review para o merge request, "
        "em português do Brasil, a partir dos findings abaixo.\n\n"
        "Regras:\n"
        "- Linha 1: veredito direto (total de findings e distribuição por severidade).\n"
        "- Depois 4 a 6 bullets: os achados mais importantes (severidade, arquivo:linha, "
        "essência em uma frase cada).\n"
        "- Última linha: uma observação técnica útil sobre o conjunto (padrão recorrente, "
        "risco dominante, etc).\n"
        "- Máximo ~140 palavras. NÃO invente achados fora da lista. Sem títulos markdown.\n\n"
        f"Revisor: {reviewer} · {len(comments)} findings:\n"
        f"{json.dumps(_compact_findings(comments), ensure_ascii=False, indent=1)}"
    )
    return chat(
        provider,
        [
            {"role": "system", "content": SUMMARY_SYSTEM},
            {"role": "user", "content": user},
        ],
    )


def fix_finding(finding: dict[str, Any], provider: str) -> str:
    """O modelo corretor gera um patch (frase + bloco de código) para o finding."""
    payload = {
        "severidade": finding.get("severity"),
        "categoria": finding.get("category"),
        "arquivo": finding.get("path"),
        "linha": finding.get("start_line"),
        "finding": finding.get("content"),
        "codigo_atual": finding.get("existing_code"),
        "sugestao_do_revisor": finding.get("suggestion_code"),
    }
    user = (
        "Corrija o finding de code review abaixo.\n\n"
        "Regras:\n"
        "- Primeiro uma frase (português do Brasil) explicando a correção.\n"
        "- Depois um bloco de código com o trecho CORRIGIDO completo — o que substitui o "
        "codigo_atual, mesmas linhas, mudança mínima, estilo do arquivo.\n"
        "- Sem refactors além do necessário para corrigir o finding.\n\n"
        f"Finding (JSON):\n{json.dumps(payload, ensure_ascii=False, indent=1)}"
    )
    return chat(
        provider,
        [
            {"role": "system", "content": FIX_SYSTEM},
            {"role": "user", "content": user},
        ],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="stage", required=True)

    p_sum = sub.add_parser("summarize", help="summary do MR escrito pelo modelo")
    p_sum.add_argument(
        "--review", type=Path, required=True, help="JSON de review do OCR"
    )
    p_sum.add_argument(
        "--provider", default="magalu", help="default: magalu (zero custo)"
    )
    p_sum.add_argument(
        "--out", type=Path, default=None, help="salva o texto em arquivo"
    )

    p_fix = sub.add_parser("fix", help="patch para um finding")
    p_fix.add_argument(
        "--review", type=Path, required=True, help="JSON de review do OCR"
    )
    p_fix.add_argument(
        "--index", type=int, default=0, help="índice do finding (default 0)"
    )
    p_fix.add_argument(
        "--provider", default="magalu", help="default: magalu (zero custo)"
    )
    p_fix.add_argument(
        "--out", type=Path, default=None, help="salva o texto em arquivo"
    )

    args = parser.parse_args(argv)
    review = json.loads(args.review.read_text(encoding="utf-8"))

    if args.stage == "summarize":
        result = summarize_findings(review, args.provider)
    else:
        comments = review.get("comments") or []
        if not comments or args.index >= len(comments):
            print(
                f"two_provider: finding [{args.index}] inexistente ({len(comments)} findings)",
                file=sys.stderr,
            )
            return 1
        result = fix_finding(comments[args.index], args.provider)

    if args.out:
        args.out.write_text(result, encoding="utf-8")
        print(f"ok -> {args.out}")
    else:
        print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
