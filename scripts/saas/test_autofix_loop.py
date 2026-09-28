"""Testes (stdlib unittest) para as funções do autofix_loop — SaaS.

Funções puras: parsing do bloco de código da resposta do fixer, aplicação do
patch por substituição do existing_code (com os casos de skip: sem bloco,
sem existing_code, trecho ausente) e o controle de continuidade do loop
(findings presentes + ciclos restantes). Gate sintático: arquivo válido
passa, nome indefinido (o caso real do MR !25) reprova, e um fix que
introduz erro estático é revertido sem chegar ao commit (fixer mockado,
gate real via ruff quando disponível).
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import autofix_loop as loop  # depois do sys.path por design
import two_provider  # para mockar o fixer

RUFF_AVAILABLE = shutil.which("ruff") is not None

FINDING = {
    "severity": "high",
    "category": "bug",
    "path": "a.py",
    "start_line": 10,
    "content": "bug propositado",
    "existing_code": "return sorted(versions)[-1]",
}


class ParseCodeBlockTest(unittest.TestCase):
    def test_extrai_ultimo_bloco_python(self):
        text = "explicação\n```python\nx = 1\n```\nmais texto"
        self.assertEqual(loop.parse_code_block(text), "x = 1")

    def test_extrai_bloco_neutro(self):
        text = "```\ny = 2\n```"
        self.assertEqual(loop.parse_code_block(text), "y = 2")

    def test_ultimo_bloco_vence(self):
        text = "```python\na\n```\nmeio\n```python\nb\n```"
        self.assertEqual(loop.parse_code_block(text), "b")

    def test_sem_bloco_retorna_none(self):
        self.assertIsNone(loop.parse_code_block("so texto, sem cercas"))


class ApplyPatchTest(unittest.TestCase):
    def test_substitui_existing_code(self):
        src = "def f():\n    return sorted(versions)[-1]\n"
        result = loop.apply_patch(src, FINDING, "return max(versions, key=semver)")
        self.assertEqual(result, "def f():\n    return max(versions, key=semver)\n")

    def test_sem_bloco_retorna_none(self):
        src = "x = 1\n"
        self.assertIsNone(loop.apply_patch(src, FINDING, None))

    def test_sem_existing_code_retorna_none(self):
        finding = dict(FINDING, existing_code=None)
        self.assertIsNone(loop.apply_patch("x = 1\n", finding, "y = 2"))

    def test_trecho_ausente_retorna_none(self):
        self.assertIsNone(loop.apply_patch("nada aqui\n", FINDING, "x = 2"))

    def test_nao_substitui_trecho_ambiguo(self):
        src = "a = 1\na = 1\n"
        finding = {"existing_code": "a = 1"}
        result = loop.apply_patch(src, finding, "a = 2")
        self.assertIsNone(result)


class ShouldContinueTest(unittest.TestCase):
    def test_com_findings_e_ciclos_restantes(self):
        self.assertTrue(loop.should_continue(1, [FINDING], 3))

    def test_sem_findings_para(self):
        self.assertFalse(loop.should_continue(1, [], 3))

    def test_no_limite_de_ciclos_para(self):
        self.assertFalse(loop.should_continue(3, [FINDING], 3))


class GateCheckTest(unittest.TestCase):
    @unittest.skipUnless(RUFF_AVAILABLE, "ruff não disponível")
    def test_arquivo_valido_passa(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "gate_sample.py"
            p.write_text("x = 1\nprint(x)\n", encoding="utf-8")
            ok, _ = loop.gate_check(p)
            self.assertTrue(ok)

    @unittest.skipUnless(RUFF_AVAILABLE, "ruff não disponível")
    def test_nome_indefinido_reprova(self):
        # caso real do MR !25: fixer usou logging sem importar
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "gate_sample.py"
            p.write_text(
                "try:\n    x = 1\nexcept OSError:\n    logging.exception('falhou')\n",
                encoding="utf-8",
            )
            ok, detail = loop.gate_check(p)
            self.assertFalse(ok)
            self.assertIn("F821", detail)


class ApplyFixesGatedTest(unittest.TestCase):
    GATED_FINDING: ClassVar[dict] = {
        "path": "pkg/mod.py",
        "existing_code": "return sorted(v)[-1]",
    }
    BASELINE: ClassVar[str] = "def f(v):\n    return sorted(v)[-1]\n"

    def _repo(self, tmp):
        repo = Path(tmp)
        (repo / "pkg").mkdir()
        (repo / "pkg" / "mod.py").write_text(self.BASELINE, encoding="utf-8")
        return repo

    @unittest.skipUnless(RUFF_AVAILABLE, "ruff não disponível")
    def test_fix_com_erro_estatico_e_revertido(self):
        bad_fix = "Correção.\n```python\nreturn logging.info(max(v))\n```"
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            with mock.patch.object(two_provider, "fix_finding", return_value=bad_fix):
                applied, skipped = loop.apply_fixes(
                    repo, [dict(self.GATED_FINDING)], "magalu"
                )
            self.assertEqual(applied, [])
            self.assertEqual(len(skipped), 1)
            self.assertIn("gate sintático", skipped[0])
            self.assertEqual(
                (repo / "pkg" / "mod.py").read_text(encoding="utf-8"),
                self.BASELINE,
            )

    def test_fix_bom_e_aplicado(self):
        good_fix = (
            "Correção.\n```python\n"
            "return max(v, key=lambda s: [int(p) for p in s.split('.')])\n```"
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            with mock.patch.object(two_provider, "fix_finding", return_value=good_fix):
                applied, skipped = loop.apply_fixes(
                    repo, [dict(self.GATED_FINDING)], "magalu"
                )
            self.assertEqual(len(applied), 1)
            self.assertEqual(skipped, [])
            content = (repo / "pkg" / "mod.py").read_text(encoding="utf-8")
            self.assertIn("max(v, key=", content)
            self.assertNotIn("sorted(v)[-1]", content)

    def test_finding_nao_altera_diretorio_irmao(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "sandbox-clone"
            sibling = root / "sandbox-clone-extra"
            repo.mkdir()
            sibling.mkdir()
            outside = sibling / "a.py"
            outside.write_text("x = 1\n", encoding="utf-8")
            finding = {"path": "../sandbox-clone-extra/a.py", "existing_code": "x = 1"}
            with mock.patch.object(two_provider, "fix_finding") as fixer:
                applied, skipped = loop.apply_fixes(repo, [finding], "magalu")
            self.assertEqual(applied, [])
            self.assertEqual(len(skipped), 1)
            self.assertEqual(outside.read_text(encoding="utf-8"), "x = 1\n")
            fixer.assert_not_called()


class ReviewContractTest(unittest.TestCase):
    def review(self):
        item = {"item_id": "1", "path": "pkg/a.py"}
        return {
            "status": "complete",
            "llm": {"provider": "deepseek", "model": "deepseek-v4-pro"},
            "comments": [],
            "manifest": {
                "schema_version": "ocr.run-manifest/v1",
                "operation": "review",
                "terminal_state": "complete",
                "input": {
                    "mode": "range",
                    "requested_from": "base",
                    "requested_head": "head",
                    "resolved_head": "head",
                },
                "execution": {"provider": "deepseek", "model": "deepseek-v4-pro"},
                "coverage": {
                    "selected": [item],
                    "completed": [item],
                    "reused": [],
                    "failed": [],
                    "waived": [],
                },
            },
        }

    def check(self, review, preview=None):
        if preview is None:
            preview = [{"path": "pkg/a.py", "will_review": True}]
        return loop.validate_review_result(
            review, preview, "base", "head", "deepseek", "deepseek-v4-pro"
        )

    def test_aceita_revisao_completa_do_modelo_e_sha_corretos(self):
        self.assertEqual(self.check(self.review()), [])

    def test_rejeita_ausencia_de_cobertura_mesmo_com_exit_zero(self):
        review = self.review()
        review["status"] = "skipped"
        review["manifest"]["terminal_state"] = "skipped"
        review["manifest"]["coverage"]["selected"] = []
        review["manifest"]["coverage"]["completed"] = []
        with self.assertRaisesRegex(RuntimeError, "review incompleto"):
            self.check(review, [])

    def test_rejeita_revisao_parcial_e_waived(self):
        for field in ("failed", "waived"):
            with self.subTest(field=field):
                review = self.review()
                review["manifest"]["coverage"][field] = [{"path": "pkg/b.py"}]
                with self.assertRaisesRegex(RuntimeError, "review incompleto"):
                    self.check(review)

    def test_rejeita_status_parcial_mesmo_sem_findings(self):
        review = self.review()
        review["status"] = "partial"
        review["manifest"]["terminal_state"] = "partial"
        with self.assertRaisesRegex(RuntimeError, "review incompleto"):
            self.check(review)

    def test_rejeita_teste_excluido_pelo_filtro(self):
        preview = [
            {"path": "pkg/a.py", "will_review": True},
            {
                "path": "pkg/test_a.py",
                "will_review": False,
                "exclude_reason": "default_path",
            },
        ]
        with self.assertRaisesRegex(RuntimeError, "review incompleto"):
            self.check(self.review(), preview)

    def test_rejeita_finding_fora_da_cobertura(self):
        review = self.review()
        review["comments"] = [{"path": "../sandbox-clone-extra/a.py"}]
        with self.assertRaisesRegex(RuntimeError, "fora da cobertura"):
            self.check(review)

    def test_rejeita_modelo_ou_sha_divergente(self):
        for location, field, value in (
            ("execution", "model", "outro"),
            ("input", "resolved_head", "outro"),
        ):
            with self.subTest(field=field):
                review = self.review()
                review["manifest"][location][field] = value
                with self.assertRaisesRegex(RuntimeError, "review incompleto"):
                    self.check(review)


class DriverV2Test(unittest.TestCase):
    def test_arquivos_com_unicode_e_quebra_de_linha_nao_sao_perdidos(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            loop.subprocess.run(["git", "init", "-q", str(repo)], check=True)
            names = ["café.py", "linha\nnova.py"]
            for name in names:
                (repo / name).write_text("x = 1\n", encoding="utf-8")
            loop.subprocess.run(["git", "add", "--", *names], cwd=repo, check=True)
            self.assertEqual(set(loop.changed_files(repo)), set(names))

    def test_ci_da_branch_deve_igualar_alvo(self):
        same = mock.Mock(returncode=0, stdout=b"trusted", stderr=b"")
        other = mock.Mock(returncode=0, stdout=b"changed", stderr=b"")
        with mock.patch.object(loop.subprocess, "run", side_effect=[same, same]):
            loop.require_trusted_ci(Path("."), "develop", "saas/demo")
        with (
            mock.patch.object(loop.subprocess, "run", side_effect=[same, other]),
            self.assertRaisesRegex(RuntimeError, "revisão humana"),
        ):
            loop.require_trusted_ci(Path("."), "develop", "saas/demo")

    def test_alvo_que_mudou_bloqueia_aprovacao(self):
        with (
            mock.patch.object(loop, "git", side_effect=[(0, ""), (0, "novo-base")]),
            self.assertRaisesRegex(RuntimeError, "origin/develop mudou"),
        ):
            loop.assert_refs_unchanged(
                Path("."), "develop", "saas/demo", "base", "head"
            )

    def test_reviewer_e_fixer_precisam_de_contas_distintas(self):
        credentials = {
            "GITLAB_REVIEWER_USERNAME": "saas-bot",
            "GITLAB_REVIEWER_PASSWORD": "reviewer-secret",
            "GITLAB_BOT_USERNAME": "saas-bot",
            "GITLAB_BOT_PASSWORD": "fixer-secret",
        }
        with (
            mock.patch.object(loop.o, "load_lab_env"),
            mock.patch.dict(loop.os.environ, credentials, clear=True),
            self.assertRaisesRegex(SystemExit, "contas distintas"),
        ):
            loop.bot_identities()

    def test_oauth_abre_e_revoga_tokens_dos_dois_bots(self):
        identities = {
            "reviewer": {"user": "saas-reviewer", "password": "review-secret"},
            "fixer": {"user": "saas-bot", "password": "fix-secret"},
        }
        app = {"id": 9}
        with (
            mock.patch.object(loop.o, "root_pat", return_value="root-pat"),
            mock.patch.object(loop.o, "delete_stale_applications"),
            mock.patch.object(loop.o, "create_application", return_value=app),
            mock.patch.object(
                loop.o,
                "password_grant",
                side_effect=[
                    (200, {"access_token": "review-token"}),
                    (200, {"access_token": "fix-token"}),
                ],
            ) as grant,
            mock.patch.object(loop.o, "revoke_token", return_value=200) as revoke,
            mock.patch.object(loop.o, "delete_application", return_value=204) as delete,
        ):
            session = loop.g6_open("http://gitlab", identities)
            self.assertEqual(
                session["tokens"],
                {"reviewer": "review-token", "fixer": "fix-token"},
            )
            self.assertEqual(
                [call.args[2] for call in grant.call_args_list],
                ["saas-reviewer", "saas-bot"],
            )
            loop.g6_close("http://gitlab", session)
        self.assertEqual(
            [call.args[2] for call in revoke.call_args_list],
            ["review-token", "fix-token"],
        )
        delete.assert_called_once_with("http://gitlab", "root-pat", 9)

    def test_oauth_do_fixer_falha_e_limpa_token_do_reviewer(self):
        identities = {
            "reviewer": {"user": "saas-reviewer", "password": "review-secret"},
            "fixer": {"user": "saas-bot", "password": "fix-secret"},
        }
        with (
            mock.patch.object(loop.o, "root_pat", return_value="root-pat"),
            mock.patch.object(loop.o, "delete_stale_applications"),
            mock.patch.object(loop.o, "create_application", return_value={"id": 9}),
            mock.patch.object(
                loop.o,
                "password_grant",
                side_effect=[(200, {"access_token": "review-token"}), (401, {})],
            ),
            mock.patch.object(loop.o, "revoke_token", return_value=200) as revoke,
            mock.patch.object(loop.o, "delete_application", return_value=204) as delete,
            self.assertRaisesRegex(RuntimeError, "OAuth do fixer falhou"),
        ):
            loop.g6_open("http://gitlab", identities)
        revoke.assert_called_once_with("http://gitlab", {"id": 9}, "review-token")
        delete.assert_called_once_with("http://gitlab", "root-pat", 9)

    def test_oauth_tenta_revogar_ambos_mesmo_se_primeiro_falhar(self):
        session = {
            "app": {"id": 9},
            "pat": "root-pat",
            "tokens": {"reviewer": "review-token", "fixer": "fix-token"},
        }
        with (
            mock.patch.object(
                loop.o, "revoke_token", side_effect=[OSError("rede"), 200]
            ) as revoke,
            mock.patch.object(loop.o, "delete_application", return_value=204) as delete,
            self.assertRaisesRegex(RuntimeError, "revogação reviewer"),
        ):
            loop.g6_close("http://gitlab", session)
        self.assertEqual(revoke.call_count, 2)
        delete.assert_called_once_with("http://gitlab", "root-pat", 9)

    def test_nota_do_fixer_falha_explicitamente_sem_acesso(self):
        with (
            mock.patch.object(loop.o, "bearer_request", return_value=(403, {})),
            self.assertRaisesRegex(RuntimeError, "nota no MR !8 falhou: HTTP 403"),
        ):
            loop.post_note("http://gitlab", 7, 8, "fix-token", "fix aplicado")

    def test_finding_do_reviewer_falha_antes_do_fix_sem_acesso(self):
        with (
            mock.patch.object(loop.o, "bearer_request", return_value=(403, {})),
            self.assertRaisesRegex(
                RuntimeError, "finding do ciclo 1 não foi publicado"
            ),
        ):
            loop.post_finding_comments(
                "http://gitlab", 7, 8, "review-token", {}, [FINDING], 1
            )

    def test_resolve_id_exato_do_reviewer(self):
        users = [{"id": 39, "username": "saas-bot"}]
        with mock.patch.object(loop, "api_request", return_value=(200, users)):
            self.assertEqual(
                loop.reviewer_user_id("http://gitlab", "tok", "saas-bot"), 39
            )

    def test_atribui_bot_no_campo_reviewer_sem_remover_outros(self):
        current = {"state": "opened", "reviewers": [{"id": 5}]}
        updated = {"state": "opened", "reviewers": [{"id": 5}, {"id": 39}]}
        with mock.patch.object(
            loop, "api_request", side_effect=[(200, current), (200, {}), (200, updated)]
        ) as request:
            loop.assign_reviewer("http://gitlab", 7, 8, "tok", 39)
        self.assertEqual(request.call_args_list[1].args[3], {"reviewer_ids": [5, 39]})

    def test_reviewer_precisa_aparecer_no_mr(self):
        current = {"state": "opened", "reviewers": []}
        with (
            mock.patch.object(
                loop,
                "api_request",
                side_effect=[(200, current), (200, {}), (200, current)],
            ),
            self.assertRaisesRegex(RuntimeError, "não aparece"),
        ):
            loop.assign_reviewer("http://gitlab", 7, 8, "tok", 39)

    def test_aprova_sha_revisado_com_identidade_do_bot(self):
        approvals = {
            "approved": True,
            "approved_by": [{"user": {"username": "saas-bot"}}],
        }
        with mock.patch.object(
            loop.o, "bearer_request", side_effect=[(201, {}), (200, approvals)]
        ) as request:
            loop.approve_review("http://gitlab", 7, 8, "oauth", "headsha", "saas-bot")
        self.assertEqual(request.call_count, 2)
        self.assertEqual(
            request.call_args_list[0].kwargs["payload"], {"sha": "headsha"}
        )

    def test_nao_considera_aprovacao_de_outra_identidade(self):
        approvals = {
            "approved": True,
            "approved_by": [{"user": {"username": "ocr-bot"}}],
        }
        with (
            mock.patch.object(
                loop.o, "bearer_request", side_effect=[(201, {}), (200, approvals)]
            ),
            self.assertRaisesRegex(RuntimeError, "não foi registrada"),
        ):
            loop.approve_review("http://gitlab", 7, 8, "oauth", "headsha", "saas-bot")

    def test_ca_do_gateway_nao_afeta_review_deepseek(self):
        review = {"comments": []}
        with mock.patch.dict("os.environ", {"OCR_ROOT_CA_FILE": "gateway.pem"}):
            with mock.patch.object(loop.subprocess, "run") as run:
                run.return_value = mock.Mock(
                    returncode=0, stdout='{"comments": []}', stderr=""
                )
                self.assertEqual(
                    loop.run_review(
                        Path("."), "base", "head", "deepseek", "deepseek-v4-pro", ""
                    ),
                    review,
                )
            self.assertNotIn("OCR_ROOT_CA_FILE", run.call_args.kwargs["env"])

    def test_pipeline_usa_ref_da_branch(self):
        with mock.patch.object(
            loop, "api_request", return_value=(201, {"id": 42})
        ) as request:
            self.assertEqual(
                loop.trigger_pipeline("http://gitlab", 7, "saas/demo", "tok"), 42
            )
        request.assert_called_once_with(
            "http://gitlab",
            "/api/v4/projects/7/pipeline",
            "tok",
            {"ref": "saas/demo"},
            "POST",
        )

    def test_pipeline_final_exige_sucesso_no_sha_revisado(self):
        for status, sha in (("failed", "head"), ("success", "other")):
            with (
                self.subTest(status=status, sha=sha),
                mock.patch.object(
                    loop,
                    "api_request",
                    return_value=(200, {"status": status, "sha": sha}),
                ),
                self.assertRaises(RuntimeError),
            ):
                loop.wait_pipeline("http://gitlab", 7, 42, "head", "tok")
        with mock.patch.object(
            loop,
            "api_request",
            return_value=(200, {"status": "success", "sha": "head"}),
        ):
            loop.wait_pipeline("http://gitlab", 7, 42, "head", "tok")

    def test_sha_final_roda_testes_e_hooks_mesmo_sem_fix(self):
        with (
            mock.patch.object(loop, "git", side_effect=[(0, "head"), (0, "")]),
            mock.patch.object(loop, "changed_files", return_value=["pkg/a.py"]),
            mock.patch.object(loop, "isolated_tests") as tests,
            mock.patch.object(loop, "trusted_precommit") as hooks,
        ):
            loop.validate_head(Path("."), "base", "head", "python3 -m unittest")
        tests.assert_called_once_with(Path("."), "head", "python3 -m unittest")
        hooks.assert_called_once_with(Path("."), ["pkg/a.py"])

    def test_sha_local_divergente_bloqueia_testes(self):
        with (
            mock.patch.object(loop, "git", return_value=(0, "outro")),
            mock.patch.object(loop.subprocess, "run") as run,
            self.assertRaisesRegex(RuntimeError, "worktree não está no SHA"),
        ):
            loop.validate_head(Path("."), "base", "head", "python3 -m unittest")
        run.assert_not_called()

    def test_auto_merge_exige_sha_revisado(self):
        mr = {"detailed_merge_status": "mergeable", "sha": "novo"}
        with (
            mock.patch.object(loop, "api_request", return_value=(200, mr)) as request,
            self.assertRaisesRegex(RuntimeError, "mudou de SHA"),
        ):
            loop.merge_converged_mr(
                "http://gitlab", 7, 8, "tok", "revisado", Path("."), "develop", "base"
            )
        self.assertEqual(request.call_count, 1)

    def test_merge_reconfere_alvo_apos_espera(self):
        mr = {
            "detailed_merge_status": "mergeable",
            "sha": "head",
            "source_branch": "saas/demo",
        }
        with (
            mock.patch.object(loop, "api_request", return_value=(200, mr)) as request,
            mock.patch.object(
                loop, "assert_refs_unchanged", side_effect=RuntimeError("alvo mudou")
            ),
            self.assertRaisesRegex(RuntimeError, "alvo mudou"),
        ):
            loop.merge_converged_mr(
                "http://gitlab", 7, 8, "tok", "head", Path("."), "develop", "base"
            )
        self.assertEqual(request.call_count, 1)

    def test_testes_falhos_bloqueiam_commit_e_precommit(self):
        with (
            mock.patch.object(loop, "git", side_effect=[(0, ""), (0, "tree")]),
            mock.patch.object(loop, "changed_files", return_value=["pkg/a.py"]),
            mock.patch.object(
                loop, "isolated_tests", side_effect=RuntimeError("testes falharam")
            ),
            mock.patch.object(loop, "trusted_precommit") as hooks,
        ):
            ok, detail = loop.validate_fix(
                Path("."), "python3 -m unittest", ["pkg/a.py"]
            )
        self.assertFalse(ok)
        self.assertIn("testes falharam", detail)
        hooks.assert_not_called()


if __name__ == "__main__":
    unittest.main()
