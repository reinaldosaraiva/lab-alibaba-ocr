# Resultados do laboratório e limites das evidências

Este dossiê separa **observação histórica**, **reprodução local possível** e
**trabalho pendente**. A edição didática traz scripts, índice dos 50 MRs e
resumos; ela não contém o gold do `review-model`, os 200 JSONs brutos do OCR,
tokens nem o banco de dados do GitLab CE de origem. Um clone novo não deve
apresentar os números abaixo como medição própria antes de repetir o método.

## 1. Loop de review e correção no GitLab CE

No projeto local `lab/sandbox`, o MR **!36** foi o controle positivo do driver
com dois provedores e duas contas OAuth:

| Etapa | Observado no host de origem |
|---|---|
| Reviewer | `deepseek-v4-pro` via DeepSeek; ciclo 1 com 2 findings e 2 comentários de `saas-reviewer` |
| Fixer | `qwen38-27b` via Magalu; 2 correções no commit `f6190ff0`, assinado por `saas-bot` |
| Nova revisão | DeepSeek: 0 findings; manifesto com 2 itens selecionados, 2 concluídos, 0 falhos e 0 dispensados |
| Qualidade | testes e pre-commit verdes; pipeline #60 `success` no SHA revisado |
| Aprovação e merge | `approved_by` continha `saas-reviewer`; merge commit `2d3eedb2` em `develop` |

O MR **!35** fornece o controle de falha: um review OCR grande excedeu o
timeout de 600 s. Não houve manifesto completo nem aprovação apresentada como
OCR; a promoção foi manual, com nota factual. Nos demos anteriores !23 e !25,
a nova revisão encontrou regressões introduzidas pelo fixer, justificando o
ciclo adicional e o gate F821 antes de cada commit.

Esses IDs são do GitLab CE em `localhost:8929` do host de pesquisa. Eles não
são links acessíveis a quem clonou o GitHub. Para produzir evidência própria,
siga o [Módulo de MR](lab-guide-student.md#módulo-5--mr-com-dois-provedores)
num sandbox seu e registre modelo, SHA, manifesto, findings, autores,
pipeline, aprovação e resultado.

**O que o pipeline comprova:** o job `saas-quality` executa testes e Ruff. O
review OCR é feito pelo driver externo e verificado pelo manifesto. Não há um
job CI chamado `ocr-review`; um badge verde de CI isolado não basta para
declarar que a revisão de modelo terminou.

## 2. S0-C: 50 MRs e 200 células

O método usou 17 PRs de `psf/requests`, 17 de `gin-gonic/gin` e 16 de
`axios/axios`, com dois modelos e esforços `medium`/`high`. O corpus e as
cotas foram fixados antes da medição.

| Estado terminal | Células | Leitura |
|---|---:|---|
| `complete` | 139 | review terminou |
| `partial` | 1 | timeout em um grupo; não conta como review completo |
| `skipped` | 60 | 15 PRs com diff sem item revisável × 4 configurações |
| `failed` | 0 | falhas transitórias reexecutadas conforme o método |

Foram consumidos **15.551.175 tokens** nos runs completos e parcial. Em
`medium`, as medianas de parede dos runs completos foram **43 s** para
`qwen38-27b` (35 runs) e **130 s** para `deepseek-v4-pro` (35 runs). A
ancoragem agregada foi **165/166 findings (99,4%)**. O consumo pay-as-you-go
registrado foi US$ 0,00 porque os endpoints estavam em planos de assinatura;
isso não mede o custo total desses planos nem de infraestrutura.

O [quadro de tokens, latência e ancoragem](../results/p001-s004-s0c/metrics.md)
traz os quatro recortes modelo × esforço. O
[quadro de precisão](../results/p001-s004-s0c/precision.md) registra 99
findings classificados como válidos em duas passagens. **As duas passagens
foram feitas pela mesma pessoa**: o acordo perfeito é consistência
intra-avaliador, não validação independente. A amostra de 99 findings não
autoriza extrapolar 100% de precisão para MRs futuros.

O índice [`data/mrs50/index.jsonl`](../data/mrs50/index.jsonl) contém refs e
metadados, sem os diffs. Para repetir o benchmark, são necessários os clones,
credenciais dos modelos, teto de tokens e tempo de execução; veja o
[atlas dos scripts](scripts.md#s0-c-50-mrs-custo-latência-e-precisão).

## 3. S0-A: filtro L1 versus base 8B

No gold unseen de 255 registros (142 limpos, 113 com defeito), o adapter
`review` teve **0/142 falsos positivos** e **2/113 acertos** (recall 1,8%).
O modelo base sem adapter teve **96/142 falsos positivos** (67,6%) e recall
de 89,4%. O critério Q4 pré-registrado passou por igualdade com o melhor piso
determinístico em Python e por piso zero em C.

**Leitura:** o adapter reduziu alertas, mas seu recall foi muito baixo. O
resultado apoia estudá-lo como filtro junto a outras camadas; não prova que
ele substitui um revisor profundo. Reproduzir S0-A requer o checkout externo
do `review-model`, o gold, o adapter e MLX. Os testes das funções de métrica
em `scripts/spike/test_s0a_report.py` rodam sem esses ativos.

## 4. S0-B: mecanismo GitLab CE

No GitLab CE 18.4.1, o laboratório observou:

- Code Quality via artefato CI apareceu no diff dos MRs de prova !16 e !17;
  SARIF foi convertido para o formato CodeClimate aceito pelo parser do CE.
- A API de Security nativo respondeu 404 nos endpoints testados; o resultado
  não deve ser confundido com a ingestão de Code Quality do CI.
- O token OAuth do bot teve TTL de **7.200 s**, refresh com rotação e escopo
  `api` necessário para commit status. Commit status e discussion inline
  funcionaram com Bearer OAuth, sem PAT nas chamadas de evidência.
- Os apps e tokens criados pelo spike foram revogados e verificados ao final.

**Pendente:** as células GitLab.com Free, inclusive assento do bot e repetição
dos mecanismos, dependem de credenciais fornecidas pelo dono. O resultado do
CE não é conclusão sobre GitLab.com. A sessão de pesquisa P001-S006 continua
BLOCKED; o estudo ad hoc do loop não muda esse estado.

## 5. O que reproduzir nesta edição

| Experimento | Comando / ponto de entrada | Evidência esperada |
|---|---|---|
| Testes e lint locais | `make study-check` | 52 testes SaaS + 3 da fixture + pre-commit verdes |
| Controle negativo F821 | Módulo 2 do guia | nome indefinido rejeitado; código corrigido aceito |
| CI da fixture | branch que altera `exercises/discount/price.py` | pipeline falha no teste; passa após correção |
| GitLab CE local | `make lab-gitlab-up` | readiness responde, `lab/sandbox` existe |
| OCR com provedor | Módulo 5 do guia | manifesto completo no SHA, comentários e nova revisão |

O resultado real de um modelo pode variar. Registre sua observação com o
modelo exato, o corpus, o SHA e o denominador antes de compará-la com este
host de referência.
