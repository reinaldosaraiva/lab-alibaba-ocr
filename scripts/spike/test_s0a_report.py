"""Testes (stdlib unittest) para as funções puras do s0a_report — S0-A.

Cobre a semântica do contrato §17 Q4 (convenção score_review, herdada por
especificação e não por import): partição clean/defect pelo gold, FP/recall
record-level, recall por eixo (eixo conta 1x por registro), CWE exato
pareado por eixo, guardas de caso vazio (None), o mapeamento de grupo de
linguagem do floor (shell/rust fora das tabelas por linguagem, dentro do
agregado) e a aritmética do veredito Q4. Sem disco/rede: só funções puras.
"""

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import s0a_report  # depois do sys.path por design


def finding(axis="security", cwe=None, severity="P2"):
    return {"axis": axis, "severity": severity, "line": 1, "cwe": cwe}


def gold_rec(language="python", findings=()):
    return {
        "language": language,
        "label": {"verdict": "pass", "findings": list(findings)},
    }


def pred_rec(*findings, valid=True):
    return {
        "strict_json": {"valid": valid},
        "label": {"verdict": "pass", "findings": list(findings)},
    }


# piso determinístico mínimo: codeql/full é o melhor recall de py; células
# n/a ou recall null não são eixo reportado (mesma forma do floor.json real)
FLOOR = {
    "slices": {
        "full": {
            "semgrep": {"py": {"status": "ok", "recall": 0.0}},
            "bandit": {"py": {"status": "ok", "recall": 0.0}},
            "codeql": {"py": {"status": "ok", "recall": 0.021739}},
        },
        "unseen": {
            "semgrep": {"py": {"status": "ok", "recall": 0.0}},
            "bandit": {"py": {"status": "n/a"}},
            "codeql": {"py": {"status": "ok", "recall": None}},
        },
    }
}


class LanguageGroupTest(unittest.TestCase):
    def test_mapeamento_do_floor(self):
        self.assertEqual(s0a_report.language_group("python"), "py")
        self.assertEqual(s0a_report.language_group("c"), "c")
        self.assertEqual(s0a_report.language_group("typescript"), "tsjs")
        self.assertEqual(s0a_report.language_group("javascript"), "tsjs")
        self.assertEqual(s0a_report.language_group("go"), "go")

    def test_sem_grupo_fica_none(self):
        self.assertIsNone(s0a_report.language_group("shell"))
        self.assertIsNone(s0a_report.language_group("rust"))
        self.assertIsNone(s0a_report.language_group(""))
        self.assertIsNone(s0a_report.language_group("kotlin"))


class PartitionTest(unittest.TestCase):
    def test_particiona_pelo_gold(self):
        gold = {
            "a": gold_rec(findings=[finding()]),
            "b": gold_rec(findings=[]),
            "c": {"language": "python"},  # sem label -> clean
        }
        clean, defect = s0a_report.partition(gold)
        self.assertEqual(clean, ["b", "c"])
        self.assertEqual(defect, ["a"])


class FpRateTest(unittest.TestCase):
    def test_clean_com_pred_finding_e_fp(self):
        gold = {"a": gold_rec(), "b": gold_rec()}
        pred = {"a": pred_rec(finding()), "b": pred_rec()}
        rate, fp, n_clean = s0a_report.fp_rate(gold, pred)
        self.assertEqual((rate, fp, n_clean), (0.5, 1, 2))

    def test_sem_clean_e_none(self):
        gold = {"a": gold_rec(findings=[finding()])}
        rate, fp, n_clean = s0a_report.fp_rate(gold, {"a": pred_rec(finding())})
        self.assertIsNone(rate)
        self.assertEqual((fp, n_clean), (0, 0))

    def test_clean_sem_pred_sai_do_denominador(self):
        gold = {"a": gold_rec(), "b": gold_rec()}
        pred = {"a": pred_rec()}  # b sem predição
        rate, fp, n_clean = s0a_report.fp_rate(gold, pred)
        self.assertEqual((rate, fp, n_clean), (0.0, 0, 1))


class RecallTest(unittest.TestCase):
    def test_defect_detectado(self):
        gold = {
            "a": gold_rec(findings=[finding()]),
            "b": gold_rec(findings=[finding()]),
        }
        pred = {"a": pred_rec(finding()), "b": pred_rec()}
        rate, detected, n_defect = s0a_report.recall(gold, pred)
        self.assertEqual((rate, detected, n_defect), (0.5, 1, 2))

    def test_sem_defect_e_none(self):
        rate, detected, n_defect = s0a_report.recall(
            {"a": gold_rec()}, {"a": pred_rec()}
        )
        self.assertIsNone(rate)
        self.assertEqual((detected, n_defect), (0, 0))


class RecallByAxisTest(unittest.TestCase):
    def test_eixo_conta_uma_vez_por_registro(self):
        gold = {
            "a": gold_rec(
                findings=[
                    finding("security"),
                    finding("security"),
                    finding("correctness"),
                ]
            )
        }
        pred = {"a": pred_rec(finding("security"))}
        by_axis = s0a_report.recall_by_axis(gold, pred)
        self.assertEqual(by_axis["security"], {"gold": 1, "hit": 1, "recall": 1.0})
        self.assertEqual(by_axis["correctness"], {"gold": 1, "hit": 0, "recall": 0.0})

    def test_defect_sem_pred_nao_enche_denominador(self):
        gold = {"a": gold_rec(findings=[finding()])}
        self.assertEqual(s0a_report.recall_by_axis(gold, {}), {})


class CweExactTest(unittest.TestCase):
    def test_pareado_por_eixo_com_cwe_igual(self):
        gold = {"a": gold_rec(findings=[finding("security", "CWE-79")])}
        pred = {"a": pred_rec(finding("security", "CWE-79"))}
        self.assertEqual(s0a_report.cwe_exact(gold, pred), (1.0, 1, 1))

    def test_cwe_diferente_conta_par_sem_acerto(self):
        gold = {"a": gold_rec(findings=[finding("security", "CWE-79")])}
        pred = {"a": pred_rec(finding("security", "CWE-89"))}
        self.assertEqual(s0a_report.cwe_exact(gold, pred), (0.0, 0, 1))

    def test_pred_sem_cwe_e_par_sem_acerto(self):
        gold = {"a": gold_rec(findings=[finding("security", "CWE-79")])}
        pred = {"a": pred_rec(finding("security", None))}
        self.assertEqual(s0a_report.cwe_exact(gold, pred), (0.0, 0, 1))

    def test_gold_sem_cwe_nao_gera_par(self):
        gold = {"a": gold_rec(findings=[finding("security", None)])}
        pred = {"a": pred_rec(finding("security", "CWE-79"))}
        self.assertEqual(s0a_report.cwe_exact(gold, pred), (None, 0, 0))

    def test_eixo_sem_par_na_pred_e_ignorado(self):
        gold = {"a": gold_rec(findings=[finding("security", "CWE-79")])}
        pred = {"a": pred_rec(finding("performance", "CWE-79"))}
        self.assertEqual(s0a_report.cwe_exact(gold, pred), (None, 0, 0))

    def test_dois_findings_do_mesmo_eixo_pareiam_uma_vez(self):
        # no pareamento por eixo, o ÚLTIMO finding do eixo no gold é o que
        # conta (dict eixo->finding, como score_review): CWE-89 pareia contra
        # o CWE-79 da pred e não acerta
        gold = {
            "a": gold_rec(
                findings=[
                    finding("security", "CWE-79"),
                    finding("security", "CWE-89"),
                ]
            )
        }
        pred = {"a": pred_rec(finding("security", "CWE-79"))}
        self.assertEqual(s0a_report.cwe_exact(gold, pred), (0.0, 0, 1))


class WilsonTest(unittest.TestCase):
    def test_limite_inferior_zero_sem_sinal_negativo(self):
        interval = s0a_report.wilson95(0.0, 100)
        self.assertEqual(interval[0], 0.0)
        # -0.0 == 0.0, então o sinal é a prova do guard de arredondamento
        self.assertEqual(math.copysign(1.0, interval[0]), 1.0)
        self.assertGreater(interval[1], 0.0)

    def test_limite_superior_um(self):
        interval = s0a_report.wilson95(1.0, 10)
        self.assertEqual(interval[1], 1.0)
        self.assertLessEqual(interval[0], 1.0)

    def test_casos_vazios_sao_none(self):
        self.assertIsNone(s0a_report.wilson95(None, 10))
        self.assertIsNone(s0a_report.wilson95(0.5, 0))


class ScoreByLanguageTest(unittest.TestCase):
    def setUp(self):
        self.gold = {
            "py1": gold_rec(findings=[finding("security", "CWE-79")]),
            "sh1": gold_rec(language="shell"),
        }
        self.pred = {
            "py1": pred_rec(finding("security", "CWE-79")),
            "sh1": pred_rec(finding()),  # FP em linguagem sem grupo
        }

    def test_agregado_inclui_registro_sem_grupo(self):
        m = s0a_report.score(self.gold, self.pred)
        self.assertEqual(m["n"], 2)
        self.assertEqual(m["missing"], 0)
        self.assertEqual((m["fp"], m["n_clean"]), (1, 1))
        self.assertEqual((m["detected"], m["n_defect"]), (1, 1))
        self.assertEqual(m["fp_rate"], 1.0)
        self.assertEqual(m["recall"], 1.0)
        self.assertEqual(m["cwe_rate"], 1.0)
        self.assertEqual(m["strict_json_valid_rate"], 1.0)

    def test_tabela_por_linguagem_exclui_sem_grupo(self):
        by_group = s0a_report.by_language(self.gold, self.pred)
        self.assertEqual(set(by_group), {"py"})
        self.assertEqual(by_group["py"]["n"], 1)
        self.assertEqual(by_group["py"]["fp"], 0)
        self.assertEqual(by_group["py"]["recall"], 1.0)

    def test_gold_sem_pred_conta_como_missing(self):
        m = s0a_report.score(self.gold, {"py1": self.pred["py1"]})
        self.assertEqual(m["n"], 1)
        self.assertEqual(m["missing"], 1)

    def test_strict_json_invalida_baixa_a_taxa(self):
        pred = {"py1": pred_rec(finding(), valid=False), "sh1": pred_rec()}
        m = s0a_report.score(self.gold, pred)
        self.assertEqual(m["strict_json_valid_rate"], 0.5)


class BestDetRecallTest(unittest.TestCase):
    def test_maximo_entre_ferramentas_e_slices(self):
        best = s0a_report.best_det_recall(FLOOR)
        self.assertEqual(best["py"]["recall"], 0.021739)
        self.assertEqual(best["py"]["source"], "codeql/full")

    def test_grupo_sem_recall_reportado_fora(self):
        best = s0a_report.best_det_recall(FLOOR)
        self.assertNotIn("c", best)
        self.assertNotIn("tsjs", best)
        self.assertNotIn("go", best)


class Q4VerdictTest(unittest.TestCase):
    @staticmethod
    def adapter_unseen(fp_rate, py_recall):
        return {"fp_rate": fp_rate, "by_group": {"py": {"recall": py_recall}}}

    def test_pass_quando_bate_piso_e_fp_baixo(self):
        verdict = s0a_report.q4_verdict(self.adapter_unseen(0.04, 0.05), FLOOR)
        self.assertEqual(verdict["verdict"], "PASS")
        self.assertTrue(verdict["fp_ok"])
        self.assertTrue(verdict["recall_ok"])
        axis = verdict["axes"][0]
        self.assertEqual(axis["group"], "py")
        self.assertEqual(axis["det_recall"], 0.021739)
        self.assertEqual(axis["det_source"], "codeql/full")
        self.assertTrue(axis["beats"])

    def test_fronteiras_sao_inclusivas(self):
        verdict = s0a_report.q4_verdict(self.adapter_unseen(0.10, 0.021739), FLOOR)
        self.assertEqual(verdict["verdict"], "PASS")

    def test_fail_quando_fp_estoura(self):
        verdict = s0a_report.q4_verdict(self.adapter_unseen(0.2, 0.05), FLOOR)
        self.assertEqual(verdict["verdict"], "FAIL")
        self.assertFalse(verdict["fp_ok"])
        self.assertTrue(verdict["recall_ok"])

    def test_fail_quando_nao_bate_piso_em_eixo_nenhum(self):
        verdict = s0a_report.q4_verdict(self.adapter_unseen(0.04, 0.01), FLOOR)
        self.assertEqual(verdict["verdict"], "FAIL")
        self.assertFalse(verdict["recall_ok"])
        self.assertTrue(verdict["fp_ok"])

    def test_fail_quando_adapter_sem_recall_no_grupo(self):
        verdict = s0a_report.q4_verdict(self.adapter_unseen(0.04, None), FLOOR)
        self.assertEqual(verdict["verdict"], "FAIL")
        self.assertIsNone(verdict["axes"][0]["adapter_recall"])
        self.assertFalse(verdict["axes"][0]["beats"])

    def test_fail_quando_fp_none(self):
        verdict = s0a_report.q4_verdict(self.adapter_unseen(None, 0.05), FLOOR)
        self.assertEqual(verdict["verdict"], "FAIL")
        self.assertFalse(verdict["fp_ok"])


if __name__ == "__main__":
    unittest.main()
