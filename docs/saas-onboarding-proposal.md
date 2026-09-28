# Proposta: instalar o review como integração GitLab

**Estado:** proposta ad hoc para discussão; nenhum endpoint, instalador ou
schema abaixo existe ainda. O experimento local em `scripts/saas/` prova o
loop com dois modelos, mas ainda exige clone, credenciais e comando manual.
Esta proposta não altera a sessão de pesquisa P001-S006, que continua
bloqueada para as células GitLab.com.

## A experiência desejada

1. O administrador informa a URL do GitLab e autoriza a integração pelo
   navegador. No GitLab.com, a aplicação OAuth do serviço é registrada uma
   vez pelo operador. Em self-managed, pode ser necessária uma aplicação
   OAuth na instância ou no grupo, com callback para o serviço.
2. O instalador descobre grupos/projetos acessíveis; a pessoa escolhe quais
   projetos ativar. Ele verifica permissões e capacidades da versão/tier.
3. O serviço provisiona identidade de bot quando suportada, cria os
   webhooks necessários e faz um teste de entrega. Exibe o que foi criado
   e como desinstalar.
4. A pessoa escolhe revisor e corretor na UI ou em `.ocr-review.yml`.
   Segredos ficam em um cofre por tenant. No modo self-hosted, podem vir
   de variáveis de ambiente ou de um secret manager local.
5. A partir daí, abrir ou atualizar um MR dispara review automaticamente.
   O serviço publica findings e um resumo. Correção, aprovação e merge são
   políticas separadas, inicialmente desligadas até validar os gates.

Isso remove do fluxo do desenvolvedor `make lab-gitlab-up`, registro manual
de runner, clone com token no `origin`, configuração de OAuth via Rails e
`python3 scripts/saas/autofix_loop.py --repo ... --test-cmd ...`. Esses
comandos continuam úteis apenas para o laboratório e diagnóstico.

## Opções de entrega

| Caminho | Instalação pelo cliente | O que aproveita | Limite |
|---|---|---|---|
| **A. Serviço hospedado + webhooks** (recomendado) | autoriza GitLab, escolhe projetos/modelos | driver OCR, validador de manifesto, poster, gates | exige serviço HTTPS alcançável e credenciais GitLab por tenant |
| **B. Componente GitLab CI** | adiciona um `include:` e variáveis protegidas no projeto/grupo | exemplo CI já existente no upstream OCR | depende de runner/CI do cliente; menor controle de identidade e de loop de autofix |
| **C. Sidecar self-hosted** | implanta o mesmo serviço na rede do GitLab | base do A, inclusive modelos locais | instalação operacional maior; necessário para ambiente sem saída à nuvem |

**Recomendação:** A como UX padrão; B como caminho de entrada simples; C
compartilha o worker e a configuração do A quando houver demanda por
air-gap. Um componente CI é útil, mas não produz sozinho a experiência de
“instalar e esquecer” comparável à de um serviço hospedado.

## Arquitetura mínima do caminho A

```text
GitLab MR event → webhook HTTPS → validação/autorização → fila por MR/SHA
  → worker efêmero (clone sem segredo no diff + OCR + manifesto)
  → findings/summary no GitLab → CI/ref gates → aprovação opcional
                                    ↓
                         fixer opt-in → teste isolado → push → re-review
```

O receptor responde rápido ao webhook; o trabalho lento fica na fila. A
chave idempotente contém instância, projeto, IID, evento e head SHA. Novo
push cancela ou invalida o job antigo. O worker valida URL/instância,
permissões, manifesto OCR, modelo, cobertura e SHA antes de publicar ou
aprovar. Segredos não entram no clone nem nos artefatos do job. Logs
registram IDs e SHAs sem tokens nem código-fonte. Código vive somente no
workspace efêmero; findings e decisões seguem a retenção definida pelo
produto.

O `autofix_loop.py` atual teria de ser dividido em componentes reutilizáveis:
GitLab transport, identidade, OCR review, publicação, fix e gates. O driver
CLI vira um cliente/teste de integração desses componentes. O caminho de
produção não deve criar uma aplicação OAuth e usar password grant a cada
MR, depender de `root_pat`, de `.lab/gitlab.json`, de `lab/sandbox` ou de um
`origin` com token embutido.

## Configuração proposta

Um arquivo versionado por repositório declara **política**, nunca senhas:

```yaml
# .ocr-review.yml — schema proposto, ainda não implementado
version: 1
review:
  provider: deepseek
  model: deepseek-v4-pro
  effort: medium
  max_tokens: 100000
fix:
  enabled: false
  provider: magalu
  model: qwen38-27b
  max_cycles: 3
approval:
  after_clean_review: false
merge:
  automatic: false
```

No serviço hospedado, as chaves de DeepSeek/Magalu e os tokens GitLab ficam
em cofre por tenant, cadastrados na UI. No sidecar, um `.env` ignorado pelo
Git pode mapear `OCR_DEEPSEEK_API_KEY`, `OCR_MAGALU_API_KEY` e
`OCR_MAGALU_CA_FILE` para o mesmo schema. A URL do provedor, CA e retenção
também são ajustes de instância/tenant, não do diff de um MR.

O instalador pode sugerir o comando de testes a partir do CI existente,
mas não deve executar um comando inferido automaticamente com segredos no
host. Autofix exige uma política de testes aprovada para o projeto e roda
em container isolado. Sem essa política, a instalação entrega **review
automático**, deixando fix e merge desativados.

## Compatibilidade GitLab e identidade

| Ambiente | Webhook | Identidade automática | Passo manual residual |
|---|---|---|---|
| GitLab.com Free | por projeto | service account disponível nas versões atuais; validar permissão de criação | autorizar app e selecionar projetos |
| GitLab.com Premium/Ultimate | por grupo ou projeto | service account; token de grupo quando política permitir | autorizar app e selecionar grupo/projetos |
| Self-managed recente | por grupo se licenciado; por projeto no Free | service account conforme versão/licença | registrar app OAuth na instância/grupo se necessário; conectar rede/CA |
| Lab CE 18.4.1 | por projeto | fluxo de duas contas do lab | provisionamento manual atual; não representa instalação universal |

Webhooks de **grupo** requerem Premium/Ultimate; por projeto funcionam no
Free. Service accounts chegaram ao Free no GitLab 18.11, portanto o CE
18.4.1 do laboratório não pode ser usado como prova dessa rota. Tokens de
service account continuam sendo tokens com expiração/rotação; “sem PAT
permanente” significa rotação automatizada, não ausência de credencial.
GitLab.com Free não oferece group access token; um instalador não pode
assumir esse fallback em todos os tiers.

Em GitLab 18.4, o webhook usa segredo compartilhado (`X-Gitlab-Token`)
comparado em tempo constante e HTTPS. A assinatura HMAC nativa foi
introduzida depois e pode ser negociada conforme a versão. O instalador
deve testar recebimento e alertar quando a instância bloqueia requests
para a URL do serviço, inclusive destinos privados em self-managed.

## Critérios para chamar isso de onboarding

1. Um usuário com permissão apropriada conecta GitLab.com e uma instância
   self-managed suportada sem editar código, sem `root_pat` e sem senha de
   bot inserida no produto.
2. O instalador registra/desinstala webhooks de modo idempotente, identifica
   versão/tier e informa claramente o fallback necessário.
3. Um MR novo e um push subsequente produzem um review por head SHA, com
   resumo e comentários atribuídos ao bot correto; retry não duplica notas.
4. Manifesto parcial, SHA divergente, provider diferente, CI vermelho ou
   mudança concorrente bloqueiam aprovação e merge.
5. Chaves dos modelos e tokens GitLab nunca aparecem no YAML versionado,
   clone, log ou artefato; rotação e revogação funcionam após desinstalar.
6. Configuração inválida explica qual campo corrigir. Uma instalação sem
   política de fix continua entregando review automático.

## Sequência de implementação

1. **Contrato e configuração:** validar `.ocr-review.yml` e cadastrar
   providers/chaves em armazenamento separado; adaptar o driver para receber
   instância, projeto, credenciais e políticas como parâmetros, sem `.lab/`.
2. **Review automático no CE:** receptor de webhook, fila idempotente,
   worker OCR e poster. Provar abertura, novo push, retry e desinstalação
   em dois projetos locais, sem exigir runner do cliente para o review.
3. **Instalador GitLab.com e self-managed:** OAuth authorization code/PKCE,
   descoberta de recursos, escolha de projetos, matriz de capacidade e
   provisionamento/revogação de hooks e identidades. Medir Free e Premium
   separadamente; não inferir um tier a partir do CE.
4. **Fix opt-in:** reutilizar testes isolados, gate F821, re-review, refs,
   pipeline e identidades distintas. Começar sem `auto-merge`; habilitar
   aprovação/merge apenas quando os controles negativos passarem no mesmo
   SHA e as regras do projeto permitirem.

## Fontes para validar na implementação

- [OAuth e fluxo de authorization code/PKCE](https://docs.gitlab.com/api/oauth2/)
- [Tipos de aplicação OAuth no GitLab](https://docs.gitlab.com/integration/oauth_provider/)
- [API de webhooks por projeto](https://docs.gitlab.com/api/project_webhooks/)
- [API de webhooks por grupo](https://docs.gitlab.com/api/group_webhooks/)
- [Service accounts e versões/tier](https://docs.gitlab.com/user/profile/service_accounts/)
- [Group access tokens e restrição no GitLab.com Free](https://docs.gitlab.com/user/group/settings/group_access_tokens/)
- [Filtro de destinos de webhook no self-managed](https://docs.gitlab.com/security/webhooks/)
- [Exemplo CI já mantido pelo OCR](https://github.com/alibaba/open-code-review/tree/main/examples/gitlab_ci)
