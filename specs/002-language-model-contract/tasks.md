# Tasks: Decouple LanguageModel e preservar providers remotos

**Input**: Design documents from `/specs/002-language-model-contract/`

**Prerequisites**: plan.md (required), spec.md (required for user stories), research.md, data-model.md, contracts/

**Tests**: Obrigatórios (issue #337 exige 10 grupos; desdobrados em 24 categorias de teste mapeadas abaixo).

**Organization**: Tasks agrupadas por user story; execução sequencial por um único agente (PR única), testes primeiro (TDD) quando aplicável.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1..US5, FDN, POL)

## Phase 1: Setup (Shared Infrastructure)

- [x] T001 Branch `refactor/language-model-contract` a partir do origin/main atualizado (992fa7c); working tree limpa.
- [x] T002 Spec Kit inicializado (`.specify/` + `.jcode/commands/`) e constituição preenchida em `.specify/memory/constitution.md`.
- [x] T003 Baselines registradas: suíte CI-equivalente backend = 2816 passed; `test_companion_runtime.py` = 31 passed.

## Phase 2: Foundational (Blocking Prerequisites)

**Propósito**: contrato canônico + adapter antes de qualquer story.

### Tests (escrever primeiro; devem FALHAR antes da implementação)

- [ ] T004 [P] [FDN] `backend/tests/test_language_model.py`: teste do contrato único — `LanguageModel` Protocol existe com `appraise`/`generate`/`describe`; `ModelSelection` frozen dataclass com provider/main/fast; `ModelFailure` com os 8 códigos; exceções canônicas carregam failure+mensagem constante; `language_failure_to_turn_code` preserva mapeamento idêntico (tabela do contracts/); `build_trusted_policy` do core produz o template canônico com BOUNDARY_RULE a partir de EmotionalStateV1/RelationshipStateV1 (sem provider).
- [ ] T005 [P] [FDN] `backend/tests/test_groq_language_model.py`: adapter preserva comportamento contratado — appraise chama `chat_completion_async` com model fast/temperature 0/json_object/max_tokens; generate chama com model main/temperature 0.8/max_tokens main; `validate_provider_input` ANTES da chamada; JSON inválido/vazio/parse fallback → `LanguageModelInvalidResponseError`; GroqPoolExhaustedError com cada failure_code → exceção canônica correspondente; GroqRequestError → `LanguageModelError` canônico; `describe()` sem chave/segredo; SDK mockado, sem rede.
- [ ] T006 [P] [FDN] `backend/tests/test_language_model_isolation.py`: teste estrutural AST — `engine.py`, `process_turn.py`, `companion_runtime.py`, `main.py`, `desktop/*` não contêm `import groq`/`from groq`/`groq_manager`/`Groq*` (allowlist: `groq_manager.py`, `groq_language_model.py`, `groq_keys.py`, tests do adapter, docs); o teste não é tão broad a ponto de banir adapter/composição (allowlist explícita).

### Implementation

- [ ] T007 [FDN] Criar `backend/language_model.py`: `LanguageModel` Protocol, `ModelSelection`, `ModelFailure`, exceções canônicas (`LanguageModelError` + concretas + `LanguageModelConfigurationError`), `language_failure_to_turn_code` (espelha provider_failure_to_turn_code), `build_trusted_policy` (template canônico único movido do companion/engine + BOUNDARY_RULE; sem I/O).
- [ ] T008 [FDN] Criar `backend/groq_language_model.py`: `GroqLanguageModel(manager, provider_config)` implementando o contrato; tradução de erros Groq→canônico na fronteira; factory `build_groq_language_model(keys, groq_params)`; sem estado por usuário.
- [ ] T009 [FDN] Rodear testes T004-T006 até verde (adaptar fixtures mínimas).

**Checkpoint**: contrato + adapter verdes isoladamente; nada do domínio ainda migrado.

## Phase 3: User Story 1 — Domínio desacoplado (P1) 🎯 MVP

**Goal**: engine/process_turn/companion_runtime falam só com o contrato.

**Independent Test**: suítes `test_process_turn.py`, `test_emotional_integration.py`, `test_bounded_turn_execution.py`, `test_companion_runtime.py` verdes com fake do contrato; teste AST verde.

### Tests first (devem falhar antes da migração)

- [ ] T010 [P] [US1] Atualizar `backend/tests/test_process_turn.py`: fakes passam a implementar o contrato único (appraise/generate/describe) e a trusted policy vem do core (`build_trusted_policy`), não do provider fake; ProcessTurn injeta contrato único.
- [ ] T011 [P] [US1] Atualizar `backend/tests/test_companion_runtime.py`: `ScriptedProvider` vira fake do contrato único; runtime recebe `language_model_factory`; nenhuma referência a ProviderPort/GroqRuntimeProvider; asserção de que a trusted policy usada no envelope é a do core.
- [ ] T012 [P] [US1] Atualizar seams em `test_emotional_integration.py` / `test_archival_memory_integration.py` / `test_bounded_turn_execution.py`: trocar mocks de `engine.groq_manager` por fake/mocks do contrato (LanguageModel) injetado no engine; asserções de chamadas preservadas (model ids, stages, budgets).

### Implementation

- [ ] T013 [US1] `backend/process_turn.py`: remover `ProviderPort` local; importar `LanguageModel` de `language_model`; `build_process_turn` continua passando o engine (que implementa o contrato).
- [ ] T014 [US1] `backend/companion_runtime.py`: remover `ProviderPort` local + `GroqRuntimeProvider` + `build_groq_runtime_provider`; usar contrato único + `build_trusted_policy` do core; `__init__` aceita `language_model`/`language_model_factory` (mantém nomes compatíveis dos params de teste onde possível); factory default lazy importando `build_groq_language_model` dentro da função.
- [ ] T015 [US1] `backend/engine.py`: remover todos os imports Groq; `__init__` aceita `language_model`/`language_model_factory` (+ retrocompat de `groq_keys` → constrói o adapter, deprecado silenciosamente ou mantido para não quebrar callers legados fora do escopo); `_appraise`/`_generate_with_messages`/`run_archival_extraction` chamam o contrato; stages tratam exceções canônicas (mapeamento preservado); expor façade `provider_status()`/`is_provider_configured` para health.
- [ ] T016 [US1] `backend/chat_engine.py`: repassar language_model ao engine (parâmetro explícito).

**Checkpoint**: domínio 100% no contrato; suítes US1 verdes.

## Phase 4: User Story 2 — Seleção explícita, sem fallback (P1)

**Goal**: composição resolve ModelSelection explícito; falha = falha do turno.

### Tests first

- [ ] T017 [P] [US2] `backend/tests/test_groq_language_model.py` (append): provider/modelo explícito em `describe()` (observabilidade sanitizada, sem segredo); nenhuma chamada a segundo provider quando o primeiro falha (fake manager que registra chamadas; assert exatamente 1 tentativa sem fallback); provider desconhecido na resolução → `LanguageModelConfigurationError` sanitizado.
- [ ] T018 [P] [US2] `backend/tests/test_bounded_turn_execution.py` (append, onde há fixtures de falha): falha do provider propagada como TurnErrorCode correto (429/503/504/500 via _map_turn_error atual) — sem retry/fallback extra.

### Implementation

- [ ] T019 [US2] `backend/groq_language_model.py`: `describe()` com ModelSelection explícito; resolução única (sem routing); erro de configuração sanitizado.
- [ ] T020 [US2] `backend/main.py`: `_map_turn_error` trata exceções canônicas do contrato (remove imports Groq); tabela de status preservada bit a bit.

**Checkpoint**: seleção explícita + sem fallback verificado.

## Phase 5: User Story 3 — Desktop local-first (P1)

**Goal**: desktop abre sem provider; adapter lazy; sem provider em idle.

### Tests first

- [ ] T021 [P] [US3] `backend/tests/test_companion_runtime.py` (append): runtime_state sem provider configurado = `provider_configured: false` sem instanciar adapter; primeiro turno sem chave → erro de configuração claro (LocalErrorCode existente `configuration`), histórico legível, persistência intacta; nenhuma construção de adapter em __init__/health/runtime_state (probe de presença só); lazy: factory chamada somente no primeiro turno.
- [ ] T022 [P] [US3] `backend/tests/test_desktop_import_isolation.py` (append/verify): import do módulo desktop/companion_runtime top-level não puxa groq SDK (só factory interna); asserções existentes de shell puro preservadas.

### Implementation

- [ ] T023 [US3] `backend/companion_runtime.py` + `backend/desktop/app.py` (se necessário): factory lazy do adapter; `runtime_state` usa probe de presença; nada de provider em idle.

**Checkpoint**: `test_companion_runtime.py` + isolamento verdes.

## Phase 6: User Story 4 — Segredos e contexto (P2)

### Tests first

- [ ] T024 [P] [US4] `backend/tests/test_language_model.py` + `test_groq_language_model.py` (append): `repr()`/`str()` das exceções e do ModelSelection sem chaves/segredos/marcadores sensíveis (caplog assert); envelope validado antes da chamada (input budget) preservado; nenhum valor de chave em nenhuma superfície (testes de sanitização existentes continuam verdes).
- [ ] T025 [P] [US4] Verificar `backend/tests/test_desktop_api.py`/`test_runtime_containment.py`: bridge não expõe key (assert existente preservada).

### Implementation

- [ ] T026 [US4] Revisão de sanitização no adapter (mensagens constantes; nunca str(exc) SDK) e nos logs (eventos de baixa cardinalidade).

## Phase 7: User Story 5 — Contrato futuro-proof mínimo (P3)

- [ ] T027 [US5] Revisão estrutural do contrato: sem `**kwargs`, sem capability flags especulativas, sem SDK types em assinaturas (parte do teste AST/estrutural T006); documentar em `docs/architecture/bounded-turn-execution.md` + `application-lifecycle.md` a nova fronteira LanguageModel (Groq deixa de ser identidade arquitetural; honestidade de que o prompt trafega ao provider remoto configurado).

## Phase 8: Composição web + health

- [ ] T028 [FDN→US1] `backend/dependencies.py`: `build_default_dependencies` constrói `GroqLanguageModel` (manager como hoje, custo idle inalterado) e injeta em `ChatConversationEngine(language_model=...)`; seleção explícita a partir de `ProviderConfig`; sem Groq acima da composição.
- [ ] T029 [P] `backend/health.py`: `ProviderCheck` consome façade do contrato no engine (`is_configured`/provider_status) — atualizar `test_health.py` mockando o contrato.

## Phase 9: Polish & Cross-Cutting

- [ ] T030 Rodar `python -m compileall -q backend`.
- [ ] T031 Rodar suíte CI-equivalente completa (17 ignores) — target ≥ 2816 passed sem falhas novas.
- [ ] T032 Rodar suítes específicas: language_model, groq_language_model, isolation, companion_runtime, process_turn, emotional_integration, bounded_turn_execution, health, app_factory, import_safety, runtime_containment, provider_models.
- [ ] T033 Smoke desktop headless: `xvfb-run -a python scripts/desktop_smoke.py`.
- [ ] T034 Frontend gates: `npm test`, `npm run lint`, `npm run build` (sem mudança funcional esperada; nenhuma API key na UI).
- [ ] T035 Auditoria Groq final: `grep` documentado na PR — símbolos Groq restritos a `groq_manager.py`, `groq_language_model.py`, composição (`dependencies.py`), testes do adapter e docs; módulos legados (`meta_cognition.py`, `turing_test.py`, `test_keys.py`, `final_verification.py`) documentados como fora de escopo (não ativos no fluxo).
- [ ] T036 Commit Conventional (`refactor(llm): ...`) + PR única contra `main` com "Closes #337" e checklist completo (contrato, ports, trusted policy, seleção, erros, segredos, testes com números, smoke, auditoria, fora de escopo).
- [ ] T037 STOP — não iniciar outra issue.

## Dependencies & Execution Order

- Phase 2 (T004-T009) bloqueia tudo (contrato + adapter).
- Phase 3 (US1) depende de Phase 2; é o MVP.
- Phases 4-8 dependem de Phase 3 (mesmos arquivos), executam em sequência.
- T017/T018/T021/T022/T024/T025 são [P] entre si (arquivos diferentes), mas cada um appended à sua suíte.
- Phase 9 é gate final antes da PR.

## Notes

- 24 categorias de teste da issue mapeadas: T004 (contrato único), T005 (adapter preserva comportamento + testável separadamente), T006 (sem Groq no domínio/AST), T010-T012 (fake determinístico sem rede), T013-T016 (engine depende do contrato), T017 (seleção explícita + sem fallback), T018 (falha=TurnErrorCode), T005/T017 (timeout/cancelamento via budget), T024 (segredos fora de repr/log), T025 (UI sem key), T021 (provider ausente = erro config claro sem quebrar app/persistência), T022 (isolamento import), T011 (trusted policy no core), T015 (mapeamento HTTP preservado), mais sanitização/atomicidade nas suítes existentes preservadas.
- Baseline de referência: 2816 passed (suíte CI-equivalente); 31 passed (companion_runtime).
- Commits pequenos por fase; PR única no fim.
