# Atlas dos scripts do laboratório

Este índice cobre os **32 arquivos Python** e a regra JSON versionados em
`scripts/`. Cada trilha indica o que o script lê, o que escreve e se chama um
serviço externo. Os comandos partem da raiz do repositório. Execute primeiro
`make study-check`; ele não chama LLM nem GitLab.

| Sinal | Efeito |
|---|---|
| **Leitura** | inspeciona arquivos ou APIs; não altera o projeto |
| **Local** | cria artefatos sob `.lab/`, `data/` ou `results/` |
| **Remoto** | chama modelos ou muda GitLab/GitHub; confira a conta e o alvo |

## Caminho de estudo recomendado

```text
study-check
    ↓
lab-ocr → lab-gitlab-up
    ↓
exercises/discount → CI saas-quality
    ↓
two_provider.py → autofix_loop.py  (credenciais e runner necessários)
    ↓
benchmark/run_floor.py → spikes S0-A / S0-B / S0-C  (trilha de pesquisa)
```

O [guia do aluno](lab-guide-student.md) executa esse caminho por etapas. O
[dossiê público](lab-results.md) distingue medição realizada de exercício
reproduzível. O `Makefile` também possui alvos de pesquisa dependentes do
`review-model` externo; `make lab-up` e `make check` não são a rota inicial da
edição didática.

## `scripts/saas/`: review, correção e gates

| Arquivo | Papel e entrada | Efeito / pré-requisito |
|---|---|---|
| [`autofix_loop.py`](../scripts/saas/autofix_loop.py) | CLI principal: `--repo` e `--branch` ou `--mr-iid`; escolhe reviewer, fixer, ciclos e `--test-cmd` | **Remoto**: cria MR, publica comentários, dispara pipeline e pode fazer merge com `--auto-merge`. Exige OCR, dois provedores, OAuth e clone com push. |
| [`two_provider.py`](../scripts/saas/two_provider.py) | `summarize` transforma findings em texto do modelo; `fix` propõe mudança para um finding | **Remoto**: usa DeepSeek ou Magalu conforme config local; `fix` não aplica o patch sozinho. |
| [`review_rule.json`](../scripts/saas/review_rule.json) | Regra OCR confiável, inclui arquivos de teste e orienta a tratar texto do MR como dado | **Leitura** pelo driver; não coloque instruções vindas do MR aqui. |
| [`test_autofix_loop.py`](../scripts/saas/test_autofix_loop.py) | Casos de seleção, manifesto, SHA, CI, fix, path e falhas | **Local**: doubles sem GitLab/modelos reais. |
| [`test_two_provider.py`](../scripts/saas/test_two_provider.py) | Casos de resumo, fixer e parsing | **Local**: doubles sem custo de modelo. |

Comandos seguros para começar:

~~~bash
python3 -m unittest discover -s scripts/saas -p 'test_*.py' -q
python3 scripts/saas/autofix_loop.py --help
python3 scripts/saas/two_provider.py --help
~~~

`autofix_loop.py` exige árvore limpa no clone do MR, compara CI com o alvo,
rejeita review parcial e aplica fixes apenas depois de validar os caminhos.
O teste em container usa snapshot `git archive`, sem rede e sem `.git` ou
segredos montados. O OCR roda fora do job CI `saas-quality`.

## `scripts/lab/`: dependências e GitLab local

| Arquivo | Papel e entrada | Efeito / pré-requisito |
|---|---|---|
| [`lab_tools.py`](../scripts/lab/lab_tools.py) | Confere versões pinadas de Go, Python, Docker, Node, Semgrep, Bandit e CodeQL | **Local**: pode instalar dependências via pip/brew e grava `.lab/tools-report.json`; rode conscientemente. |
| [`gitlab_bootstrap.py`](../scripts/lab/gitlab_bootstrap.py) | Aguarda GitLab CE, cria group `lab`, projeto `sandbox`, `ocr-bot` e token | **Remoto local**: altera o GitLab e guarda credenciais em `.lab/gitlab.json`. `make lab-gitlab-up` o invoca. |
| [`l1_check.py`](../scripts/lab/l1_check.py) | Confere hashes do adapter e do gold do `review-model` | **Leitura**: depende do checkout externo de pesquisa; não é requisito do guia básico. |
| [`lab_manifest.py`](../scripts/lab/lab_manifest.py) | Registra versões e hashes do ambiente completo | **Local**: grava `results/lab-manifest.json`; requer o `review-model` externo. |

`make lab-ocr` compila o clone pinado de `alibaba/open-code-review`; o código
dele fica em `open-code-review/`, ignorado por Git. O patch versionado em
[`patches/`](../patches/ocr-root-ca-file.patch) habilita uma CA do gateway do
lab. `make lab-gitlab-up` sobe o Compose e chama o bootstrap. Uma nova chamada
ao bootstrap pode rotacionar tokens; depois de reiniciar Docker, use o Compose
diretamente conforme o guia.

## `scripts/benchmark/`: piso determinístico

| Arquivo | Papel e entrada | Efeito / pré-requisito |
|---|---|---|
| [`run_floor.py`](../scripts/benchmark/run_floor.py) | Executa Semgrep, Bandit ou CodeQL sobre o gold e seu slice unseen; calcula FPR, recall e Wilson 95% | **Local**: grava `floor.json`; exige gold e manifesto externos à edição didática. |
| [`test_run_floor.py`](../scripts/benchmark/test_run_floor.py) | Testa parsing, agregação e controles do benchmark | **Local**: `python3 -m unittest discover -s scripts/benchmark -p 'test_*.py' -q`. |

O `floor.json` não é um ranking universal: o corpus, as versões das regras e
o denominador por linguagem determinam as taxas. Recalcule apenas com o gold
original e o manifesto compatível.

## `scripts/spike/`: experimentos da Fase 0

### S0-C: 50 MRs, custo, latência e precisão

| Arquivo | Papel e entrada | Efeito / pré-requisito |
|---|---|---|
| [`mrs50_select.py`](../scripts/spike/mrs50_select.py) | Seleciona PRs merged nos três projetos e cotas do método | **Remoto**: consulta GitHub API; grava `data/mrs50/index.jsonl`. Reexecutar hoje pode mudar o corpus porque a janela e os projetos são fixos, mas os dados da API podem mudar. |
| [`mrs50_clone.py`](../scripts/spike/mrs50_clone.py) | Clona os projetos do index e busca refs dos PRs | **Remoto/local**: rede GitHub e espaço em disco; cria clones sob `.qwen/`. |
| [`s0c_batch.py`](../scripts/spike/s0c_batch.py) | Executa a matriz 50 × 2 modelos × 2 esforços | **Remoto**: até 200 reviews e milhões de tokens; exige provedores, OCR e teto de consumo. Não é exercício inicial. |
| [`s0c_status.py`](../scripts/spike/s0c_status.py) | Resume tentativas e estados das células | **Leitura**: precisa de `runs.jsonl` e JSONs brutos gerados pelo batch. |
| [`s0c_report.py`](../scripts/spike/s0c_report.py) | Calcula tokens, latência, findings e ancoragem | **Leitura** dos runs brutos; a edição didática publica resultados resumidos, sem raw JSON. |
| [`s0c_sample.py`](../scripts/spike/s0c_sample.py) | Sorteia amostra estratificada de findings | **Local**: lê runs brutos e grava amostra. |
| [`s0c_annotate_sheet.py`](../scripts/spike/s0c_annotate_sheet.py) | Monta folha de anotação humana | **Local**: requer amostra e diffs. |
| [`s0c_precision.py`](../scripts/spike/s0c_precision.py) | Valida anotações e computa precisão/kappa | **Local**: exige anotações; o resultado publicado usou duas passagens do mesmo avaliador. |
| [`fetch_gateway_cert.py`](../scripts/spike/fetch_gateway_cert.py) | Lê o certificado do gateway Magalu | **Remoto/local**: grava `.lab/magalu-ca.pem`; valide a origem da CA antes de confiar nela. |
| [`llm_probe.py`](../scripts/spike/llm_probe.py) | Uma chamada mínima a endpoint OpenAI-compatible | **Remoto**: consome tokens; útil só com credenciais próprias. |
| [`local_llm_proxy.py`](../scripts/spike/local_llm_proxy.py) | Proxy TLS local usado no spike histórico | **Local/remoto**: encaminha chamadas ao gateway em loopback e valida a CA em `.lab/magalu-ca.pem`. Não faz parte do fluxo normal do driver. |
| [`proxy_test.py`](../scripts/spike/proxy_test.py) | Exercita o proxy com uma completion | **Remoto**: consome tokens; diagnóstico do spike histórico. |

O corpus já selecionado está em
[`data/mrs50/index.jsonl`](../data/mrs50/index.jsonl). É apenas um índice de
refs e metadados, sem diffs; os 200 JSONs brutos ficam fora desta edição.

### S0-A: adapter L1 versus modelo base

| Arquivo | Papel e entrada | Efeito / pré-requisito |
|---|---|---|
| [`s0a_batch.py`](../scripts/spike/s0a_batch.py) | Inferência local nas quatro células base/adapter × unseen/mrs50 | **Local**: requer `review-model` com MLX, gold e adapter pinados; execução histórica levou horas. |
| [`s0a_report.py`](../scripts/spike/s0a_report.py) | FP, recall e comparação com o piso determinístico | **Local**: lê predições e `floor.json`; não infira qualidade só de FP baixo. |
| [`test_s0a_report.py`](../scripts/spike/test_s0a_report.py) | Testes de métricas e casos de borda | **Local**: não requer MLX. |

### S0-B: integração GitLab CE e OAuth

| Arquivo | Papel e entrada | Efeito / pré-requisito |
|---|---|---|
| [`s0b_ce_probe.py`](../scripts/spike/s0b_ce_probe.py) | Sonda endpoints e tiers de ingestão no CE | **Remoto local**: cria artefatos de evidência no sandbox GitLab. |
| [`s0b_runner_ci.py`](../scripts/spike/s0b_runner_ci.py) | Registra runner e prova Code Quality por CI artifact e conversão SARIF | **Remoto local**: cria runner, pipelines e MRs de probe; o runner tem socket Docker. |
| [`s0b_oauth.py`](../scripts/spike/s0b_oauth.py) | Mede grant, TTL, refresh e escopos OAuth | **Remoto local**: cria app/tokens e os revoga ao fim; não rode contra GitLab.com sem plano de limpeza. |
| [`s0b_oauth_bot.py`](../scripts/spike/s0b_oauth_bot.py) | Repete a medição com usuário bot dedicado | **Remoto local**: exige conta bot provisionada. |
| [`s0b_external.py`](../scripts/spike/s0b_external.py) | Publica commit status e discussion com Bearer OAuth | **Remoto local**: cria MR de prova e revoga tokens. |
| [`s0b_report.py`](../scripts/spike/s0b_report.py) | Gera tabela mecanismo × tier e disposições provisórias | **Local**: lê os JSONs de evidência, ausentes da edição didática. |
| [`test_s0b_report.py`](../scripts/spike/test_s0b_report.py) | Testes de parsing, redação e relatório | **Local**: não usa GitLab. |

Os scripts S0-B têm autoridade de administrador no **GitLab CE descartável**.
O estudo básico usa o relatório em [`lab-results.md`](lab-results.md); a
repetição do spike completo é tarefa do instrutor. GitLab.com Free continua
pendente na pesquisa, sem execução escondida por este guia.

## Que checks posso rodar sem credenciais?

~~~bash
make study-check
python3 -m unittest discover -s scripts/benchmark -p 'test_*.py' -q
python3 -m unittest discover -s scripts/spike -p 'test_*.py' -q
~~~

Esses comandos exercitam código de métrica, relatório, driver e a fixture de
desconto. Nenhum deles cria MR, chama LLM ou religa o GitLab. Os scripts de
spike que fazem writes remotos devem ser executados individualmente, depois
de ler seu `--help`, num projeto descartável.
