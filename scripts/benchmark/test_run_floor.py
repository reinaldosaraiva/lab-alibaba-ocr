"""Testes (stdlib unittest) para run_floor.py — correções da review S003.

Cobre a disposition de P001-S003 (plans/P001-S003-results.md): semântica de
unscorable (MED), guard de drift de gold e preservação de notes/unscorable
no _merge_slices, validação de record_id no _rebuild e limite inferior de
Wilson sem -0.0. Não toca disco do repo: tmp dirs via tempfile.
"""

import io
import json
import math
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_floor  # depois do sys.path por design

# diff mínimo reconstruível: primeiro hunk pós-mutação com 3 linhas
DIFF_PY = "@@ -1,2 +1,3 @@\n def foo():\n     pass\n+    return 1\n"


def make_record(record_id, language, *, block=None, findings=None, diff=None):
    """Registro mínimo de gold: só os campos que o código sob teste lê."""
    record = {"id": record_id, "language": language}
    if block is not None:
        record["block"] = block
    if findings is not None:
        record["label"] = {"findings": findings}
    if diff is not None:
        record["diff"] = diff
    return record


class UnscorableIdsTest(unittest.TestCase):
    """MED: clean sem reconstrução também é unscorable (antes deflava FPR)."""

    def test_inclui_clean_sem_recon_e_defect_fora_de_range(self):
        corpus = [
            make_record("aaaa000000000001", "python", block="clean"),
            make_record("aaaa000000000002", "python", block="clean"),
            make_record(
                "aaaa000000000003",
                "python",
                findings=[{"line": 2, "cwe": "CWE-79"}],
            ),
            make_record(
                "aaaa000000000004",
                "python",
                findings=[{"line": 99, "cwe": "CWE-79"}],
            ),
            make_record("aaaa000000000005", "python", findings=[{"cwe": "CWE-79"}]),
        ]
        # 0002 (clean) não foi reconstruído; 0004 tem linha anotada além do
        # total; 0005 é defect sem linha anotada (range não verificável)
        line_counts = {
            "aaaa000000000001": 10,
            "aaaa000000000003": 10,
            "aaaa000000000004": 10,
            "aaaa000000000005": 10,
        }
        self.assertEqual(
            run_floor._unscorable_ids(corpus, line_counts),
            ["aaaa000000000002", "aaaa000000000004", "aaaa000000000005"],
        )

    def test_exclui_registro_sem_grupo_de_linguagem(self):
        # fora do escopo (ex. shell): sem grupo NÃO entra em unscorable —
        # já é reportado em excluded_languages e nunca é pontuado
        corpus = [make_record("aaaa000000000009", "shell", block="clean")]
        self.assertEqual(run_floor._unscorable_ids(corpus, {}), [])


class MergeSlicesTest(unittest.TestCase):
    """LOW: guard de drift de gold + preservação de notes/unscorable."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out_path = Path(tmp.name) / "floor.json"

    def _write_floor(self, floor):
        self.out_path.write_text(json.dumps(floor), encoding="utf-8")

    def test_recusa_sha_de_gold_divergente(self):
        self._write_floor({"gold": {"sha256": "b" * 64}, "notes": ["antiga"]})
        with redirect_stderr(io.StringIO()) as stderr, self.assertRaises(SystemExit):
            run_floor._merge_slices(
                self.out_path, "semgrep", {"full": {}}, [], [], "a" * 64
            )
        self.assertIn("outro gold", stderr.getvalue())

    def test_sha_igual_preserva_e_mescla(self):
        self._write_floor(
            {
                "gold": {"sha256": "a" * 64},
                "notes": ["nota antiga", "repetida"],
                "unscorable": ["bbbb000000000002"],
                "slices": {"full": {"bandit": {"py": {"status": "ok"}}}},
                "codeql_packs": {"python": "1.2.3"},
            }
        )
        slices, existing, notes, unscorable = run_floor._merge_slices(
            self.out_path,
            "semgrep",
            {"full": {"py": {"status": "ok"}}},
            ["repetida", "nota nova"],
            ["aaaa000000000001"],
            "a" * 64,
        )
        # notes: união deduplicada, anteriores primeiro, atuais depois
        self.assertEqual(notes, ["nota antiga", "repetida", "nota nova"])
        self.assertEqual(unscorable, ["aaaa000000000001", "bbbb000000000002"])
        self.assertEqual(sorted(slices["full"]), ["bandit", "semgrep"])
        self.assertEqual(existing["codeql_packs"], {"python": "1.2.3"})

    def test_sem_floor_ou_sem_sha_merge_normal(self):
        # saída inexistente: só os dados da run corrente
        _, existing, notes, unscorable = run_floor._merge_slices(
            self.out_path,
            "semgrep",
            {"full": {}},
            ["n1"],
            ["aaaa000000000003"],
            "a" * 64,
        )
        self.assertEqual(existing, {})
        self.assertEqual(notes, ["n1"])
        self.assertEqual(unscorable, ["aaaa000000000003"])
        # gold.sha256 None: drift não verificável, não trava
        self._write_floor({"gold": {"sha256": None}, "notes": ["antiga"]})
        _, _, notes, _ = run_floor._merge_slices(
            self.out_path, "semgrep", {"full": {}}, ["n1"], [], "a" * 64
        )
        self.assertEqual(notes, ["antiga", "n1"])


class WilsonTest(unittest.TestCase):
    """LOW: rate=0 não pode produzir -0.0 (nem resíduo negativo)."""

    def test_rate_zero_sem_zero_negativo(self):
        for n in range(1, 51):
            with self.subTest(n=n):
                lower, _upper = run_floor._wilson(0.0, n)
                self.assertGreaterEqual(lower, 0.0)
                # pega também o caso -0.0, que passa no >= (−0.0 == 0.0)
                self.assertEqual(math.copysign(1.0, lower), 1.0)


class RebuildTest(unittest.TestCase):
    """LOW: record_id do gold é validado antes de virar path component."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.work = Path(tmp.name) / "work"

    def test_record_id_invalido_vira_not_built(self):
        good = make_record("0123456789abcdef", "python", diff=DIFF_PY)
        bad = make_record("../escape", "python", diff=DIFF_PY)
        _, line_counts, not_built = run_floor._rebuild(self.work, [good, bad])
        self.assertEqual(not_built, ["../escape"])
        self.assertEqual(line_counts, {"0123456789abcdef": 3})
        self.assertEqual(
            sorted(entry.name for entry in (self.work / "py").iterdir()),
            ["0123456789abcdef"],
        )
        self.assertTrue((self.work / "py" / "0123456789abcdef" / "file.txt").is_file())

    def test_valid_record_id(self):
        self.assertTrue(run_floor._valid_record_id("0123456789abcdef"))
        self.assertTrue(run_floor._valid_record_id("0123456789abcdef-v2"))
        self.assertFalse(run_floor._valid_record_id("../escape"))
        self.assertFalse(run_floor._valid_record_id("0123456789abcde"))
        self.assertFalse(run_floor._valid_record_id("0123456789ABCDEF"))


class PrintSummaryTest(unittest.TestCase):
    """LOW: os dois 'excluídos' ganham rótulos distintos no resumo."""

    def test_rotulos_distintos_para_excluidos(self):
        floor = {
            "sets": {"defect": 5, "clean": 3, "excluded": 40},
            "gold": {"n": 548},
            "unseen": {"n": 2},
            "slices": {},
            "piso_det": {group: None for group in run_floor.GROUPS},
            "excluded_languages": {"shell": 6, "rust": 3},
        }
        with redirect_stdout(io.StringIO()) as stdout:
            run_floor._print_summary("semgrep", floor, Path("floor.json"), 100)
        output = stdout.getvalue()
        self.assertIn("excluídos sem findings (defect-manual): 40", output)
        self.assertIn("fora do escopo de linguagem: shell 6, rust 3", output)


if __name__ == "__main__":
    unittest.main()
