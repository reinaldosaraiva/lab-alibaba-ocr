"""Testes (stdlib unittest) para as funções puras do s0b_report — S0-B.

Cobre a redação de segredos (redact/fingerprint: nenhuma forma de token
PAT/Bearer/hex-64 sobrevive, mas o fingerprint curto sim, e a operação é
idempotente), a formatação de TTL, o veredito dos probes de Security
(403/404 → n/a por design; 200 → inesperado; ausente → pendente), a
montagem das células da tabela (GitLab.com BLOQUEADA fixa, CE derivada só
dos JSONs, placeholder quando insumo falta) e a renderização da
tabela/relatório a partir de dicts sintéticos — incluindo a garantia de que
redact(render_report(...)) remove um token plantado no insumo. Sem
disco/rede: só funções puras.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import s0b_report  # depois do sys.path por design

# valores sintéticos com o FORMATO dos segredos reais (nunca segredos)
FAKE_GLPAT = "glpat-" + "a1B2c3D4e5" * 3
FAKE_BEARER_TOKEN = "Z" * 64
FAKE_HEX64 = "ab" * 32
FP = s0b_report.fingerprint("segredo-sintetico")

CE_PROBE = {
    "generated_at": "2026-09-25T13:50:00Z",
    "security_endpoints": [
        {
            "endpoint": "/api/v4/projects/1/vulnerabilities",
            "status": 403,
            "message": "not licensed for vulnerability findings",
        },
        {
            "endpoint": "/api/v4/projects/1/security_findings",
            "status": 403,
            "message": "not licensed",
        },
        {
            "endpoint": "/api/v4/projects/1/vulnerability_findings",
            "status": 404,
            "message": "Not Found",
        },
        {
            "endpoint": "/api/v4/projects/1/scans",
            "status": 403,
            "message": "not licensed",
        },
    ],
    "pipeline": {
        "ref": "main",
        "default_branch": "main",
        "seeded_initial_commit": True,
        "seed_identity": "root",
        "created": {"http": 201, "id": 7, "status": "pending"},
        "poll_samples": [
            {"elapsed_s": 0.0, "status": "pending"},
            {"elapsed_s": 5.1, "status": "pending"},
        ],
        "cancel": {"http": 200, "status_after": "canceled"},
    },
    "runners": {"project_runners": 0, "note": "sem runner"},
}

OAUTH = {
    "generated_at": "2026-09-25T14:00:00Z",
    "app": {
        "id": 3,
        "name": "s0b-oauth-test",
        "secret_sha256": FP,
    },
    "password_grant": {
        "scopes": ["api", "read_user"],
        "expires_in": 7200,
        "created_at": 1790000000,
        "user_check": {"http": 200, "username": "root"},
    },
    "refresh_grant": {
        "access_token_changed": True,
        "refresh_token_rotated": True,
        "expires_in": 7200,
    },
    "scope_test": {
        "scopes": ["read_user"],
        "get_user_status": 200,
        "commit_status": {
            "status": 403,
            "message": "insufficient_scope",
            "expected": 403,
            "passed": True,
        },
    },
    "revocation": {
        "tokens": [{"which": "api_grant_access", "http": 200}],
        "delete_application": {"http": 204},
        "verify": {"get_application": 404, "access_token_rejected": 401},
    },
    "timings": {"password_grant": 0.31},
}

EXTERNAL = {
    "generated_at": "2026-09-25T14:05:00Z",
    "app": {"id": 4, "name": "s0b-external-test"},
    "steps": {
        "ensure_default": "default existente (main)",
        "branch": {"name": "s0b/ext-x", "from": "main"},
        "commit": {"sha": "c" * 40, "file": "s0b-test.md", "http": 201},
        "status": {
            "http": 201,
            "state": "success",
            "context": "s0b/external",
            "created": True,
        },
        "mr": {"iid": 2, "state": "opened", "http": 201},
        "discussion": {
            "id": "d1",
            "http": 201,
            "marker": "<!-- ocr-summary -->",
        },
        "readback": {
            "discussions_http": 200,
            "discussion_author": "root",
            "statuses_http": 200,
            "status_state": "success",
            "status_context": "s0b/external",
        },
    },
    "evidence_g6": {
        "auth": "Bearer (OAuth2 password grant, app s0b-external-test)",
        "pat_used_in_evidence_calls": False,
        "token_identity": "root",
        "discussion_author": "root",
    },
    "cleanup": {
        "mr_closed": True,
        "branch_deleted": True,
        "tokens_revoked": [{"which": "access", "http": 200}],
        "app_deleted_http": 204,
        "app_verify_404": True,
    },
}

RUNNER_CI = {
    "runner": {"name": "s0b-lab-runner", "id": 20, "status": "started"},
    "cells": {
        "ci_artifact_gl_schema": {
            "mr_iid": 16,
            "pipeline": {"id": 40, "status": "success"},
            "code_quality_mr_diff": {
                "exists": True,
                "content": {
                    "merge_request_16": {
                        "files": {
                            "app.py": [
                                {
                                    "line": 10,
                                    "description": "S0-B probe: SQL injection",
                                    "severity": "major",
                                }
                            ]
                        }
                    }
                },
            },
        },
        "ci_artifact_sarif": {
            "mr_iid": 17,
            "pipeline": {"id": 42, "status": "success"},
            "code_quality_mr_diff": {
                "exists": True,
                "content": {
                    "merge_request_17": {
                        "files": {
                            "app.py": [
                                {
                                    "line": 10,
                                    "description": "SARIF probe",
                                    "severity": "critical",
                                }
                            ]
                        }
                    }
                },
            },
        },
    },
}

OAUTH_BOT = {
    "bot_user": "reinaldo.saraiva",
    "bot_user_id": 35,
    "bot_user_state": "active",
    "app": {
        "id": 3,
        "name": "s0b-oauth-bot",
        "secret_sha256": "sha256:958d419bd6b8",
    },
    "password_grant": {
        "expires_in": 7200,
        "user_check": {
            "http": 200,
            "username": "reinaldo.saraiva",
            "is_bot_user": True,
        },
    },
    "refresh_grant": {
        "access_token_changed": True,
        "refresh_token_rotated": True,
        "expires_in": 7200,
    },
    "scope_test": {
        "get_user_status": 200,
        "commit_status": {"status": 403, "message": "higher privileges"},
    },
    "revocation": {
        "tokens": [{"which": "api_grant_access", "http": 200}],
        "delete_application": {"http": 204},
        "verify": {"get_application": 404, "access_token_rejected": 401},
    },
    "generated_at": "2026-09-26T14:06:43Z",
}

MECHANISMS = {
    "ingest: artefato de CI (schema GitLab)",
    "ingest: SARIF via conversor",
    "ingest: API direta",
    "oauth: app + password grant (TTL)",
    "oauth: refresh + rotação",
    "oauth: escopo mínimo p/ commit status",
    "external: commit status sem PAT",
    "external: discussion inline sem PAT",
}


class FingerprintTest(unittest.TestCase):
    def test_formato_prefixo_sha256_12_hex(self):
        self.assertRegex(s0b_report.fingerprint("x"), r"^sha256:[0-9a-f]{12}$")

    def test_deterministico_e_distinto(self):
        self.assertEqual(s0b_report.fingerprint("abc"), s0b_report.fingerprint("abc"))
        self.assertNotEqual(
            s0b_report.fingerprint("abc"), s0b_report.fingerprint("abd")
        )


class RedactTest(unittest.TestCase):
    def test_pat_glpat_e_redigido(self):
        out = s0b_report.redact(f"token {FAKE_GLPAT} no meio")
        self.assertNotIn(FAKE_GLPAT, out)
        self.assertIn("[REDACTED sha256:", out)

    def test_bearer_e_redigido_maiuscula_e_minuscula(self):
        for header in (
            f"Authorization: Bearer {FAKE_BEARER_TOKEN}",
            f"authorization: bearer {FAKE_BEARER_TOKEN}",
        ):
            out = s0b_report.redact(header)
            self.assertNotIn(FAKE_BEARER_TOKEN, out)
            self.assertIn("[REDACTED sha256:", out)

    def test_hex64_e_redigido(self):
        out = s0b_report.redact(f"access={FAKE_HEX64};")
        self.assertNotIn(FAKE_HEX64, out)

    def test_fingerprint_curto_sobrevive(self):
        text = f"secret {FP} gravado como evidência"
        self.assertEqual(s0b_report.redact(text), text)

    def test_idempotente(self):
        text = f"{FAKE_GLPAT} {FAKE_HEX64}"
        once = s0b_report.redact(text)
        self.assertEqual(s0b_report.redact(once), once)

    def test_texto_normal_intocado(self):
        text = "pipeline 7 canceled; MR !2 fechado"
        self.assertEqual(s0b_report.redact(text), text)


class FmtTtlTest(unittest.TestCase):
    def test_horas_minutos(self):
        self.assertEqual(s0b_report.fmt_ttl(7200), "7200s (2h)")
        self.assertEqual(s0b_report.fmt_ttl(3600), "3600s (1h)")
        self.assertEqual(s0b_report.fmt_ttl(3720), "3720s (1h2min)")
        self.assertEqual(s0b_report.fmt_ttl(60), "60s (1min)")

    def test_none_e_n_a(self):
        self.assertEqual(s0b_report.fmt_ttl(None), "n/a")


class ProbeVerdictTest(unittest.TestCase):
    def test_so_403_404_e_n_a_por_design(self):
        verdict = s0b_report.probe_verdict(CE_PROBE)
        self.assertIn("n/a por design", verdict)
        self.assertIn("Ultimate-only", verdict)
        self.assertIn("→403", verdict)

    def test_200_e_inesperado(self):
        probe = {
            "security_endpoints": [
                {"endpoint": "/api/v4/projects/1/scans", "status": 200}
            ]
        }
        self.assertIn("inesperado", s0b_report.probe_verdict(probe))

    def test_status_misto_fica_pendente(self):
        probe = {
            "security_endpoints": [
                {"endpoint": "/a", "status": 403},
                {"endpoint": "/b", "status": 500},
            ]
        }
        self.assertIn("_(pendente)_", s0b_report.probe_verdict(probe))

    def test_ausente_ou_vazio_e_pendente(self):
        self.assertIn("_(pendente)_", s0b_report.probe_verdict(None))
        self.assertIn(
            "_(pendente)_", s0b_report.probe_verdict({"security_endpoints": []})
        )


class BuildCellsTest(unittest.TestCase):
    def test_oito_linhas_e_mecanismos(self):
        cells = s0b_report.build_cells(CE_PROBE, OAUTH, EXTERNAL)
        self.assertEqual(len(cells), 8)
        self.assertEqual({cell["mechanism"] for cell in cells}, MECHANISMS)

    def test_gitlab_com_sempre_bloqueada(self):
        for cells in (
            s0b_report.build_cells(CE_PROBE, OAUTH, EXTERNAL),
            s0b_report.build_cells(None, None, None),
        ):
            for cell in cells:
                self.assertEqual(cell["free"], s0b_report.BLOCKED_CELL)
                self.assertEqual(cell["premium"], s0b_report.BLOCKED_CELL)

    def test_celulas_ce_derivam_dos_dados(self):
        cells = {
            cell["mechanism"]: cell["ce"]
            for cell in s0b_report.build_cells(CE_PROBE, OAUTH, EXTERNAL)
        }
        self.assertIn("7200s (2h)", cells["oauth: app + password grant (TTL)"])
        self.assertIn("rotação de refresh: sim", cells["oauth: refresh + rotação"])
        self.assertIn(
            "POST statuses 403", cells["oauth: escopo mínimo p/ commit status"]
        )
        self.assertIn("state=success", cells["external: commit status sem PAT"])
        self.assertIn("@root", cells["external: discussion inline sem PAT"])
        self.assertIn("n/a por design", cells["ingest: API direta"])
        self.assertIn("sem runner", cells["ingest: artefato de CI (schema GitLab)"])

    def test_evidencia_presente_em_toda_linha(self):
        for cell in s0b_report.build_cells(CE_PROBE, OAUTH, EXTERNAL):
            self.assertTrue(cell["evidence"])

    def test_insumo_ausente_vira_pendente_sem_quebrar(self):
        cells = {
            cell["mechanism"]: cell["ce"]
            for cell in s0b_report.build_cells(CE_PROBE, None, None)
        }
        self.assertIn(
            "_(pendente)_ oauth.json ausente",
            cells["oauth: app + password grant (TTL)"],
        )
        self.assertIn(
            "_(pendente)_ external.json ausente",
            cells["external: commit status sem PAT"],
        )


class RenderTableTest(unittest.TestCase):
    def test_cabecalho_e_uma_linha_por_celula(self):
        cells = s0b_report.build_cells(CE_PROBE, OAUTH, EXTERNAL)
        lines = s0b_report.render_table(cells)
        self.assertTrue(lines[2].startswith("| mecanismo |"))
        self.assertIn(s0b_report.TIER_FREE, lines[2])
        self.assertEqual(len(lines), 4 + len(cells))

    def test_linhas_fecham_com_pipe_e_bloqueio(self):
        cells = s0b_report.build_cells(CE_PROBE, OAUTH, EXTERNAL)
        for line in s0b_report.render_table(cells)[4:]:
            self.assertTrue(line.endswith(" |"))
            self.assertIn(s0b_report.BLOCKED_CELL, line)


class DispositionTest(unittest.TestCase):
    def test_a5_com_dados_e_provisorio(self):
        text = s0b_report.a5_disposition(OAUTH)
        self.assertIn("A5 PROVISÓRIA", text)
        self.assertIn("7200s (2h)", text)
        self.assertIn("_(resume)_", text)

    def test_a9_aponta_ultimate_only(self):
        text = s0b_report.a9_disposition(CE_PROBE)
        self.assertIn("Ultimate-only", text)
        self.assertIn("_(resume)_", text)

    def test_g6_evidencia_autor_sem_pat(self):
        text = s0b_report.g6_disposition(EXTERNAL)
        self.assertIn("sem PAT", text)
        self.assertIn("@root", text)
        self.assertIn("_(resume)_", text)

    def test_sem_dados_fica_pendente(self):
        for func in (
            s0b_report.a5_disposition,
            s0b_report.a9_disposition,
            s0b_report.g6_disposition,
        ):
            self.assertIn("_(pendente)_", func(None))


class RenderReportTest(unittest.TestCase):
    def test_estrutura_e_bloqueio(self):
        report = s0b_report.render_report(CE_PROBE, OAUTH, EXTERNAL, "X")
        self.assertTrue(report.startswith("# S0-B"))
        self.assertTrue(report.endswith("\n"))
        self.assertIn(s0b_report.BLOCKED_CELL, report)
        self.assertIn("## (a) Ingestão", report)
        self.assertIn("## (b) OAuth", report)
        self.assertIn("## (c) Commit status", report)
        self.assertIn("## Revogação", report)
        self.assertIn("canceled", report)

    def test_sem_nenhum_insumo_nao_quebra(self):
        report = s0b_report.render_report(None, None, None, "X")
        self.assertIn("_(pendente)_", report)
        self.assertIn(s0b_report.BLOCKED_CELL, report)

    def test_redact_remove_token_plantado_no_insumo(self):
        poisoned = dict(OAUTH)
        poisoned["app"] = {**OAUTH["app"], "secret_sha256": FAKE_GLPAT}
        raw = s0b_report.render_report(CE_PROBE, poisoned, EXTERNAL, "X")
        # o render é puro e passa o valor adiante; a defesa é o redact
        self.assertIn(FAKE_GLPAT, raw)
        self.assertNotIn(FAKE_GLPAT, s0b_report.redact(raw))

    def test_relatorio_inclui_secao_a2_com_runner_ci(self):
        report = s0b_report.render_report(
            CE_PROBE, OAUTH, EXTERNAL, "X", runner_ci=RUNNER_CI
        )
        self.assertIn("## (a2) Ingestão Code Quality", report)
        self.assertIn("MR !16", report)


class RunnerCiTest(unittest.TestCase):
    def test_ci_artifact_cell_sem_runner_ci_e_pendente(self):
        cell = s0b_report._ci_artifact_cell(None, "ci_artifact_gl_schema")
        self.assertIn("_(pendente)_", cell)
        self.assertIn("runner-ci.json", cell)

    def test_ci_artifact_cell_com_runner_ci_mostra_diff(self):
        cell = s0b_report._ci_artifact_cell(RUNNER_CI, "ci_artifact_gl_schema")
        self.assertIn("MR !16", cell)
        self.assertIn("app.py:10", cell)
        self.assertIn("severity major", cell)
        self.assertIn("pipeline 40 success", cell)
        self.assertIn("Ultimate-only", cell)

    def test_ci_artifact_cell_sarif_severidade_critica(self):
        cell = s0b_report._ci_artifact_cell(RUNNER_CI, "ci_artifact_sarif")
        self.assertIn("MR !17", cell)
        self.assertIn("severity critical", cell)

    def test_build_cells_com_runner_ci_preenche_celulas_de_ci(self):
        cells = {
            c["mechanism"]: c["ce"]
            for c in s0b_report.build_cells(
                CE_PROBE, OAUTH, EXTERNAL, runner_ci=RUNNER_CI
            )
        }
        self.assertIn("MR !16", cells["ingest: artefato de CI (schema GitLab)"])
        self.assertIn("MR !17", cells["ingest: SARIF via conversor"])

    def test_render_ci_artifact_section(self):
        section = "\n".join(s0b_report.render_ci_artifact_section(RUNNER_CI))
        self.assertIn("runner", section)
        self.assertIn("MR !16", section)
        self.assertIn("MR !17", section)
        self.assertIn("severity=critical", section)

    def test_render_ci_artifact_section_sem_dados(self):
        section = "\n".join(s0b_report.render_ci_artifact_section(None))
        self.assertIn("_(pendente)_", section)

    def test_a9_com_runner_ci_menciona_code_quality(self):
        text = s0b_report.a9_disposition(CE_PROBE, runner_ci=RUNNER_CI)
        self.assertIn("Code Quality via CI artifact funciona", text)
        self.assertIn("MR !16", text)
        self.assertIn("_(resume)_", text)


class OauthBotTest(unittest.TestCase):
    def test_secao_bot_mostra_identidade_e_ttl(self):
        section = "\n".join(s0b_report.render_oauth_bot_section(OAUTH_BOT))
        self.assertIn("reinaldo.saraiva", section)
        self.assertIn("is_bot_user=True", section)
        self.assertIn("7200s (2h)", section)
        self.assertIn("id 35", section)

    def test_secao_bot_sem_dados_e_pendente(self):
        section = "\n".join(s0b_report.render_oauth_bot_section(None))
        self.assertIn("_(pendente)_", section)

    def test_a5_com_bot_seat_menciona_bot_user(self):
        text = s0b_report.a5_disposition(OAUTH, oauth_bot=OAUTH_BOT)
        self.assertIn("cell (b) bot seat", text)
        self.assertIn("reinaldo.saraiva", text)

    def test_relatorio_inclui_secao_b2_com_bot(self):
        report = s0b_report.render_report(
            CE_PROBE, OAUTH, EXTERNAL, "X", oauth_bot=OAUTH_BOT
        )
        self.assertIn("## (b2)", report)
        self.assertIn("reinaldo.saraiva", report)

    def test_relatorio_sem_bot_nao_quebra(self):
        report = s0b_report.render_report(CE_PROBE, OAUTH, EXTERNAL, "X")
        self.assertIn("_(pendente)_", report)


if __name__ == "__main__":
    unittest.main()
