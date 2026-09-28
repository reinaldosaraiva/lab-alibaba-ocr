"""Testes (stdlib unittest) para os estágios do two_provider — SaaS dois providers.

Sem disco/rede: chat() mockado captura o prompt e devolve texto sintético.
Cobre a montagem dos prompts (findings compactados com path/severidade e
identificação do revisor; finding com arquivo/linha/código atual), o
truncamento de conteúdo longo, a passagem do texto do modelo, o erro limpo
quando o provider não tem config e o CLI (summarize com --out em tmp; fix
com índice inexistente).
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import two_provider  # depois do sys.path por design

REVIEW = {
    "llm": {"provider": "deepseek", "model": "deepseek-chat"},
    "comments": [
        {
            "severity": "high",
            "category": "bug",
            "path": "scripts/spike/x.py",
            "start_line": 10,
            "content": "DELETE sem escopo apaga runners de outros tenants",
            "existing_code": "DELETE FROM ci_runners;",
            "suggestion_code": "DELETE FROM ci_runners WHERE name = 'x';",
        },
        {
            "severity": "low",
            "category": "style",
            "path": "Makefile",
            "start_line": 1,
            "content": "target sem .PHONY",
        },
    ],
}


class SummarizeTest(unittest.TestCase):
    def test_prompt_contem_findings_e_retorna_texto_do_modelo(self):
        captured = {}

        def fake_chat(provider, messages, max_tokens=1600, timeout=240):
            captured["provider"] = provider
            captured["user"] = messages[-1]["content"]
            return "SUMARIO DO MODELO"

        with mock.patch.object(two_provider, "chat", side_effect=fake_chat):
            result = two_provider.summarize_findings(REVIEW, "magalu")

        self.assertEqual(result, "SUMARIO DO MODELO")
        self.assertEqual(captured["provider"], "magalu")
        self.assertIn("scripts/spike/x.py", captured["user"])
        self.assertIn("high", captured["user"])
        self.assertIn("deepseek/deepseek-chat", captured["user"])
        self.assertIn("2 findings", captured["user"])

    def test_content_longo_e_truncado_em_220(self):
        review = {
            "llm": {"provider": "p", "model": "m"},
            "comments": [
                {
                    "severity": "low",
                    "category": "other",
                    "path": "a.py",
                    "start_line": 1,
                    "content": "X" * 500,
                }
            ],
        }
        captured = {}

        def fake_chat(provider, messages, max_tokens=1600, timeout=240):
            captured["user"] = messages[-1]["content"]
            return "ok"

        with mock.patch.object(two_provider, "chat", side_effect=fake_chat):
            two_provider.summarize_findings(review, "magalu")

        self.assertIn("X" * 220, captured["user"])
        self.assertNotIn("X" * 221, captured["user"])


class FixTest(unittest.TestCase):
    def test_prompt_contem_finding_e_codigo_atual(self):
        captured = {}

        def fake_chat(provider, messages, max_tokens=1600, timeout=240):
            captured["user"] = messages[-1]["content"]
            return "PATCH DO MODELO"

        with mock.patch.object(two_provider, "chat", side_effect=fake_chat):
            result = two_provider.fix_finding(REVIEW["comments"][0], "magalu")

        self.assertEqual(result, "PATCH DO MODELO")
        for expected in ("scripts/spike/x.py", "10", "DELETE FROM ci_runners;"):
            self.assertIn(expected, captured["user"])


class ChatConfigTest(unittest.TestCase):
    def test_provider_sem_config_erro_limpo(self):
        with (
            mock.patch.object(two_provider, "load_providers", return_value={}),
            self.assertRaises(SystemExit),
        ):
            two_provider.chat("inexistente", [{"role": "user", "content": "x"}])


class CliTest(unittest.TestCase):
    def test_summarize_cli_escreve_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            review = Path(tmp) / "review.json"
            out = Path(tmp) / "summary.md"
            review.write_text(json.dumps(REVIEW), encoding="utf-8")

            with mock.patch.object(two_provider, "chat", return_value="TEXTO"):
                rc = two_provider.main(
                    ["summarize", "--review", str(review), "--out", str(out)]
                )

            self.assertEqual(rc, 0)
            self.assertEqual(out.read_text(encoding="utf-8"), "TEXTO")

    def test_fix_index_inexistente_retorna_erro(self):
        with tempfile.TemporaryDirectory() as tmp:
            review = Path(tmp) / "review.json"
            review.write_text(json.dumps(REVIEW), encoding="utf-8")

            rc = two_provider.main(["fix", "--review", str(review), "--index", "9"])

            self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
