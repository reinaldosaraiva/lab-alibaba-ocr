# Laboratório de AI Code Review com GitLab CE

Este repositório reúne um laboratório de revisão de código com o
[Open Code Review](https://github.com/alibaba/open-code-review), GitLab CE local
e um loop experimental com dois provedores: um modelo revisa o merge request e
outro propõe correções. O [guia de estudo](docs/lab-guide-student.md) conduz os
exercícios, mostra o que foi observado e separa a prática local das etapas que
exigem contas de modelos.

## Comece pelo guia

1. Leia [pré-requisitos e limites](docs/lab-guide-student.md#antes-de-começar).
2. Execute os [testes sem credenciais](docs/lab-guide-student.md#módulo-1--entender-o-loop-sem-chamar-modelos).
3. Se quiser subir o GitLab CE, siga o
   [Módulo 2](docs/lab-guide-student.md#módulo-2--subir-o-gitlab-ce-local).
4. Use o [Módulo 4](docs/lab-guide-student.md#módulo-4--observar-um-merge-request-com-dois-provedores)
   apenas depois de configurar os provedores e as duas identidades OAuth.

## Mapa

| Caminho | Conteúdo |
|---|---|
| `scripts/saas/` | revisão, resumo, correção, publicação, testes e gates do loop |
| `scripts/lab/` | bootstrap do GitLab CE e verificação do ambiente |
| `lab/gitlab/compose.yaml` | GitLab CE e runner para o laboratório |
| `patches/` | patch aplicado ao OCR pinado para confiar na CA do gateway do lab |
| `scripts/benchmark/` e `scripts/spike/` | experimentos de benchmark e spikes da Fase 0 |
| `results/` | evidências medidas; os resultados brutos não são necessários para o guia |
| `plans/` | histórico Reentry no clone de pesquisa; não integra a edição pública |

## O que foi demonstrado

No GitLab CE local, um MR de demonstração passou por dois ciclos: o
`deepseek-v4-pro` produziu dois findings, o `qwen38-27b` corrigiu os dois, a
segunda revisão não encontrou novos problemas, o pipeline de qualidade passou
e o bot revisor aprovou o merge. O job CI atual se chama `saas-quality`; a
revisão OCR roda fora do CI. O benchmark de 50 MRs gerou 200 células
modelo × esforço, incluindo 139 runs completos, 1 parcial e 60 sem diff
revisável. Consulte o guia para interpretar denominadores e limites.

Isto é um **laboratório de pesquisa**, não um serviço SaaS pronto para
terceiros. O GitLab.com Free ainda depende de credenciais para concluir o
spike de integração. Não publique senhas, tokens, saídas de `.lab/` ou URLs de
Git remotas com credenciais.

O código do OCR não é copiado para cá: o guia clona a versão pinada do projeto
upstream. Consulte a licença Apache-2.0 no repositório upstream.
