# Feature Specification: Decouple LanguageModel e preservar providers remotos

**Feature Branch**: `refactor/language-model-contract`

**Created**: 2026-09-03

**Status**: Draft

**Input**: User description: "P1 (issue #337): desacoplar LanguageModel e preservar providers remotos no desktop — um único contrato pequeno de LanguageModel derivado dos call sites reais, Groq atrás de adapter explícito, sem auto-routing/fallback, segredos fora do frontend, desktop local-first preservado."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Katherine conversa com qualquer provider remoto sem acoplar domínio (Priority: P1)

A Katherine Core (ConversationEngine, fluxo de turno, política confiável, memória, estado emocional) conversa apenas com o contrato `LanguageModel`. O adapter Groq atual (e qualquer provider remoto futuro) é plugado explicitamente na composição. Um fake determinístico do contrato substitui o provider nos testes de domínio, sem rede.

**Why this priority**: É o cerne da issue: Katherine não é um modelo específico; remove Groq como identidade arquitetural sem reescrever persona/memória/engine, e abre caminho para adapter local futuro sem tocar o domínio.

**Independent Test**: Teste unitário injeta um fake `LanguageModel` no fluxo do turno (web e desktop) e verifica appraisal + geração + erros tipados sem qualquer import de SDK. Teste estrutural (AST) garante que módulos de domínio não importam símbolos Groq.

**Acceptance Scenarios**:

1. **Given** o engine web e o runtime desktop com um fake `LanguageModel` injetado, **When** um turno completa, **Then** appraisal e geração passam exclusivamente pelo contrato, e o resultado persistido é idêntico ao atual.
2. **Given** o código de domínio (`engine.py`, `process_turn.py`, `companion_runtime.py` fora do adapter), **When** a análise estrutural roda, **Then** nenhum símbolo `Groq*`/`from groq` aparece acima do adapter/composição.
3. **Given** uma falha do provider, **When** ela atravessa o contrato, **Then** chega como erro canônico tipado (código de baixa cardinalidade), nunca como exceção SDK crua.

---

### User Story 2 - O usuário escolhe explicitamente provider/modelo; falha não troca de provider (Priority: P1)

A seleção de provider/modelo é explícita e observável de forma sanitizada (identificação de provider/modelo para observabilidade, sem segredos). Quando o provider selecionado falha, o turno falha conforme o contrato; nunca há auto-routing nem fallback para outro provider/modelo. Provider desconhecido falha sanitizado com erro de configuração claro.

**Why this priority**: Requisito explícito da issue ("Configuração deve ser explícita. Nunca escolher outro provider/modelo automaticamente quando o selecionado falhar.") e princípio de não-engano do produto.

**Independent Test**: Teste injeta um `LanguageModel` que falha e verifica que o turno falha com o código canônico correto e que nenhum segundo provider é consultado; teste de seleção desconhecida verifica erro sanitizado de configuração.

**Acceptance Scenarios**:

1. **Given** provider `groq` com modelo explícito configurado, **When** a chamada falha com rate limit/timeout/erro de servidor, **Then** o turno falha com o código canônico correspondente e nenhum outro provider é chamado.
2. **Given** seleção de provider desconhecido, **When** a composição resolve o provider, **Then** falha imediata, sanitizada, com erro de configuração claro (sem stack, sem nome de modelo cru, sem chave).
3. **Given** o contrato, **When** o adapter reporta identificação para observabilidade, **Then** expõe apenas provider+modelo explícitos, nunca chaves/prefixos.

---

### User Story 3 - Desktop abre e funciona local-first com ou sem provider remoto (Priority: P1)

O app desktop abre sem login, sem Supabase e sem provider configurado. Histórico SQLite é legível sem provider. Nenhuma requisição de provider em idle/startup, sem threads de provider em background; o adapter remoto é criado lazy no primeiro turno que precisa dele. Provider ausente gera erro de configuração claro sem quebrar persistência local nem a abertura do app.

**Why this priority**: Preserva os comportamentos das issues #334/#335/#336 que não podem regredir; é o requisito 10 dos testes obrigatórios.

**Independent Test**: Testes de runtime state/health sem provider configurado + smoke headless do desktop (`scripts/desktop_smoke.py` sob `xvfb-run`) + teste de isolamento de import (backend.desktop não puxa SDK remoto).

**Acceptance Scenarios**:

1. **Given** nenhuma chave de API configurada, **When** o app desktop abre, **Then** abre normalmente, histórico é legível, e o primeiro turno sem provider retorna erro de configuração sanitizado (persistência intacta).
2. **Given** o app desktop em idle (sem turno), **When** nada acontece, **Then** nenhuma requisição/retry de provider ocorre e nenhum worker de provider vive.
3. **Given** chaves válidas configuradas via env/arquivo Python-side, **When** o primeiro turno executa, **Then** o adapter é criado nesse momento (lazy) e a chamada usa provider/modelo explícitos.

---

### User Story 4 - Segredos e conteúdo permanecem sob controle do lado Python (Priority: P2)

API keys existem apenas no lado Python (env/arquivo com permissões restritas quando aplicável), nunca no bundle React, retornos da bridge, `repr`, logs ou mensagens de erro. Quando um provider remoto está configurado, somente o contexto necessário (envelope validado, com budgets/seleção) trafega, e a documentação é honesta de que o prompt vai ao provider.

**Why this priority**: Requisito de segurança/sanitização não negociável da issue e da constituição do projeto; P2 porque é comportamento majoritariamente existente que o refactor não pode regredir.

**Independent Test**: Testes de sanitização verificam ausência de marcadores sensíveis em logs/repr/respostas; teste estrutural verifica que a bridge não expõe chaves; auditoria de referências Groq restringe símbolos ao adapter/composição/testes do adapter.

**Acceptance Scenarios**:

1. **Given** um turno que falha, **When** logs e resposta de erro são inspecionados, **Then** nenhum valor de chave, prefixo, prompt ou conteúdo de usuário aparece.
2. **Given** um provider remoto configurado, **When** o envelope é construído, **Then** apenas o contexto validado/limitado é enviado (budgets existentes preservados), nunca o banco/histórico inteiro.
3. **Given** o contrato `LanguageModel`, **When** qualquer implementação o implementa, **Then** objetos de SDK não atravessam a fronteira (mensagens estruturadas simples entram, texto/código tipado saem).

---

### User Story 5 - Contrato futuro-proof mínimo: adapter local pode entrar sem tocar domínio (Priority: P3)

O contrato cobre apenas o que o fluxo real usa hoje (geração com mensagens estruturadas, appraisal com JSON, limites explícitos, timeout/deadline, cancelamento, identificação sanitizada, erros tipados mínimos). Sem `**kwargs`, sem capability flags especulativas, sem abstração universal. Um adapter local futuro implementa a mesma interface sem alterar domínio/engine.

**Why this priority**: Garante o critério de aceite "futuro adapter local pode ser adicionado sem alterar domínio" mantendo o contrato deliberadamente pequeno; P3 porque o adapter local em si está fora de escopo.

**Independent Test**: Revisão estrutural do contrato (uma interface pequena, sem kwargs/SDK) + teste com fake demonstrando a costura; a PR documenta itens fora de escopo (LLM local, auto-routing, marketplace) sem implementá-los.

**Acceptance Scenarios**:

1. **Given** o contrato publicado, **When** um novo adapter é escrito, **Then** ele depende apenas de tipos do contrato (dados primitivos/estruturados), sem tocar ConversationEngine/persona/memória.
2. **Given** o contrato, **When** observado, **Then** não contém abstrações para providers imagináveis além do comportamento já usado pela Katherine.

---

### Edge Cases

- Provider selecionado indisponível no meio do turno: o turno falha com código canônico; commit/replay/atomicidade permanecem como hoje (nenhum retry de provider além dos limites explícitos existentes).
- Cancelamento do request (HTTP ou desktop): propaga como hoje via budget/CancelledError, mapeado para código canônico `cancelled`/`turn_timeout` conforme o comportamento atual.
- Adapter criado lazy e a primeira chamada falha por chave inválida: erro de configuração sanitizado; app continua aberto, histórico legível, próximo turno pode tentar de novo.
- Envelope inválido localmente (budget de input excedido): falha antes de qualquer chamada ao provider (comportamento atual preservado).
- Resposta vazia/JSON inválido do provider: erro canônico `provider_invalid_response`, sem texto cru do provider na superfície.
- Concorrência: múltiplos turnos simultâneos usam o mesmo contrato sem estado compartilhado por usuário (estado por requisição, como hoje).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: O sistema DEVE expor um único contrato pequeno `LanguageModel` (interface canônica) cobrindo exclusivamente: geração com mensagens estruturadas, appraisal (JSON), limites explícitos de input/output, timeout/deadline, cancelamento, identificação sanitizada de provider/modelo e erros tipados mínimos.
- **FR-002**: O núcleo de domínio (ConversationEngine, ProcessTurn, CompanionRuntime fora do adapter) DEVE depender apenas do contrato; nenhum símbolo de SDK Groq (GroqClientManager, GroqRequestError, GroqPoolExhaustedError, ProviderFailure, ChatCompletion, HTTP details) pode aparecer acima do adapter.
- **FR-003**: A política confiável (trusted policy) DEVE ser responsabilidade do núcleo Katherine, extraída do contrato de provider para o domínio (função/core canônico), reutilizando a lógica e o template existentes sem mudança de comportamento.
- **FR-004**: Os dois `ProviderPort` duplicados (web `process_turn.py` e desktop `companion_runtime.py`) DEVEM ser consolidados em um único contrato canônico usado por ambos os fluxos.
- **FR-005**: O adapter Groq DEVE traduzir erros do SDK para erros canônicos do contrato dentro da fronteira do adapter (incluindo a taxonomia de falha existente: rate_limited, auth_failed, connection_failed, server_error, invalid_request, invalid_response, timeout, cancelled), preservando o mapeamento atual para TurnErrorCode/códigos HTTP.
- **FR-006**: A seleção de provider/modelo DEVE ser explícita (config/parâmetro explícito na composição); o sistema NÃO DEVE fazer auto-routing nem fallback para outro provider/modelo; provider desconhecido DEVE falhar de forma sanitizada com erro de configuração claro.
- **FR-007**: Falha do provider selecionado DEVE resultar em falha do turno conforme o contrato, com códigos de baixa cardinalidade e mensagens constantes, sem texto de exceção bruto, detalhes de HTTP/SDK, chaves ou conteúdo de usuário em qualquer superfície (log, repr, resposta, bridge).
- **FR-008**: API keys DEVEM permanecer somente no lado Python (env/arquivo local com permissões restritas quando arquivo for usado), fora do bundle React, dos retornos da bridge, de `repr`, logs e mensagens de erro; nenhuma sincronização automática de credenciais.
- **FR-009**: O desktop DEVE abrir e permanecer utilizável (histórico SQLite legível, UI operante) sem provider remoto configurado; provider ausente produz erro de configuração claro sem quebrar persistência local.
- **FR-010**: NENHUMA requisição, retry, thread ou worker de provider DEVE existir em idle/startup do desktop; a construção do adapter DEVE ser lazy (primeiro uso).
- **FR-011**: O contrato DEVE preservar os limites de contexto/privacidade existentes: somente o envelope validado com budgets/seleção é enviado ao provider remoto, nunca banco/histórico inteiro.
- **FR-012**: O runtime desktop DEVE manter atomicidade/replay do commit local, allowlist da bridge, timeouts/cancelamento e modo web intactos (sem regressão dos comportamentos #334/#335/#336).
- **FR-013**: O contrato DEVE ser testável com um fake determinístico sem rede; testes de domínio NÃO DEVEM exigir SDK/keys reais.
- **FR-014**: Os 10 grupos de testes obrigatórios da issue DEVEM estar cobertos (contrato único; adapter preserva comportamento; seleção explícita; sem fallback; timeout/cancelamento; segredos fora de repr/log/resposta; UI sem key; fake determinístico; adapter testável separadamente; provider ausente não quebra app/persistência), incluindo teste estrutural (AST) que bloqueia imports Groq no domínio sem banir adapter/composição.
- **FR-015**: Documentação de arquitetura DEVE refletir a nova fronteira LanguageModel quando tocar os fluxos descritos (Groq deixa de ser identidade arquitetural onde for o caso), sendo honesta de que o prompt trafega ao provider remoto configurado.

### Key Entities *(include if feature involves data)*

- **LanguageModel (contrato)**: Interface canônica do backend para geração de linguagem. Métodos: appraisal (mensagem do usuário + budget → appraisal estruturado) e geração (mensagens estruturadas validadas + budget → texto), com identificação sanitizada de provider/modelo. Erros: taxonomy canônica tipada (códigos de falha de baixa cardinalidade). Sem estado por usuário.
- **ModelFailure (taxonomia de erro)**: Códigos canônicos de falha de modelo (rate_limited, auth_failed, connection_failed, server_error, invalid_request, invalid_response, timeout, cancelled) com mapeamento preservado para os códigos de turno/HTTP existentes. Sem texto de exceção bruta.
- **ModelSelection (seleção explícita)**: Valor explícito de provider (hoje: `groq`) e modelo (ids de modelo explícitos da configuração existente), resolvido na composição; desconhecido falha sanitizado.
- **TrustedPolicyBuilder (política confiável)**: Responsabilidade do núcleo Katherine: constroi a política de sistema confiável a partir de estado emocional/relacional tipado e instruções de atuação, com a boundary rule; extraída do contrato de provider para o core, reutilizando template/lógica atuais.
- **GroqLanguageModel (adapter)**: Implementação do contrato que fala com o serviço Groq atual via manager de clientes/retries/limits existentes; criação lazy; traduz erros SDK→ModelFailure na fronteira; única morada de símbolos Groq (junto com composição e testes do adapter).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Um único contrato `LanguageModel` existe no backend; ambos os fluxos (web e desktop) o utilizam; os dois `ProviderPort` duplicados deixam de existir (verificável por grep/estrutura).
- **SC-002**: Auditoria de referências Groq mostra símbolos Groq restritos ao adapter, composição (composition root web/desktop), testes do adapter e documentação; zero referências na lógica de domínio (verificado por teste AST + grep documentado na PR).
- **SC-003**: Suíte de testes unitários do CI (sem integração Supabase) passa com números ≥ baseline (baseline: 2816 passed) sem enfraquecimento de testes; suí touchadas passam individualmente.
- **SC-004**: Smoke headless do desktop passa (`xvfb-run` + `scripts/desktop_smoke.py`); app abre sem provider configurado.
- **SC-005**: Frontend não é tocado funcionalmente; `npm test`, `npm run lint`, `npm run build` passam inalterados ou apenas com o que o contrato exige (nenhuma API key alcança a UI — verificável por teste/grep).
- **SC-006**: Nenhuma dependência nova foi adicionada; `pip check` continua limpo.
- **SC-007**: Comportamento contratado preservado: mapeamento atual de códigos de falha→TurnErrorCode→HTTP se mantém idêntico (testado explicitamente).
- **SC-008**: Nenhum LLM local, auto-routing, fallback automático ou marketplace foi implementado (fora de escopo documentado na PR).

## Assumptions

- A arquitetura desktop (#334/#335/#336) está estável e mesclada; esta issue não mistura provider com migração de shell/storage.
- O provider remoto atual é Groq; permanece capability de primeira classe atrás do adapter. LLM local NÃO é requisito desta issue.
- A configuração de chaves continua sendo env/arquivo Python-side (loaders existentes `groq_keys`/settings), sem keyring e sem sincronização automática; sem nova dependência.
- Os testes de integração Supabase rodam apenas no job `database` da CI com stack local; localmente, a suíte CI-equivalente (sem eles) é o gate de validação, com baseline 2816 passed.
- Módulos legados não ativos (ex.: `meta_cognition.py`, `turing_test.py`, `test_keys.py`) não fazem parte do fluxo; alterações neles ficam fora de escopo exceto se a auditoria estrutural exigir (documentado, não implementado).
- Web legado (Supabase/FastAPI/migrations) recebe mudanças mínimas necessárias apenas para consumir o novo contrato.
- A localização dos artefatos de spec usa o scaffold atual (`.specify/` + `specs/`), já que o diretório `specs/` da era #336 foi removido do main.
