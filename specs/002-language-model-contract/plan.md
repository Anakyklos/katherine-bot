# Implementation Plan: Decouple LanguageModel e preservar providers remotos

**Branch**: `refactor/language-model-contract` | **Date**: 2026-09-03 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/002-language-model-contract/spec.md`

**Note**: This template is filled in by the `/speckit.plan` command; its definition describes the execution workflow.

## Summary

Criar um único contrato canônico `LanguageModel` no backend, derivado dos call sites reais (appraisal JSON + geração com mensagens estruturadas), consolidar os dois `ProviderPort` duplicados (web/desktop) nesse contrato, mover a construção da trusted policy para o núcleo, e colocar o Groq atrás de um adapter explícito que traduz os erros do SDK para a taxonomia canônica existente na fronteira. Zero `**kwargs`, zero objetos de SDK atravessando a fronteira, seleção explícita sem auto-routing/fallback, segredos Python-side, desktop local-first preservado (lazy adapter, sem provider em idle), sem novas dependências. Uma PR fecha a issue #337.

## Technical Context

**Language/Version**: Python 3.12 (venv do repo, `.venv`), JavaScript/React no frontend (não tocado funcionalmente).

**Primary Dependencies**: fastapi, groq 1.5.0 (SDK, apenas dentro do adapter), httpx, pywebview 5.4 (desktop shell), anyio/pytest 8.2.1 (testes). Nenhuma dependência nova.

**Storage**: Supabase (web legado, inalterado), SQLite local via `backend.local_storage` (desktop, inalterado).

**Testing**: pytest com `asyncio_mode=strict` (pytest.ini), suíte CI-equivalente = `python -m pytest backend/tests --ignore=<17 arquivos de integração Supabase>`; baseline 2816 passed. Frontend: vitest + eslint + build. Desktop smoke: `xvfb-run python scripts/desktop_smoke.py`.

**Target Platform**: Linux (desktop pywebview + web FastAPI), CI ubuntu-latest.

**Project Type**: web-service + desktop companion app (hybrid).

**Performance Goals**: Sem aumento de custo idle no desktop: nenhum import de SDK no startup (lazy adapter), nenhuma thread/worker de provider em idle. Import time do desktop shell não regride (testes de isolamento existentes).

**Constraints**: Sem novas dependências; estado de usuário nunca em singleton global; contratos públicos e dados persistidos preservados; PR pequena e revisável; só issue #337.

**Scale/Scope**: ~5.3k LOC de backend tocados indiretamente; arquivos-alvo: `backend/language_model.py` (novo), `backend/groq_language_model.py` (novo adapter), `backend/engine.py`, `backend/process_turn.py`, `backend/companion_runtime.py`, `backend/main.py`, `backend/dependencies.py`/`settings.py` (composição), testes novos + ajustes pontuais.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Princípio | Status | Justificativa |
|---|---|---|
| I. Contrato único por fronteira | PASS | Um único `LanguageModel`; ProviderPorts duplicados consolidados; adapter explícito para Groq. |
| II. Domínio isento de provedor (Test-First) | PASS | Domínio importa só o contrato; fake determinístico em testes; teste AST bloqueia Groq no domínio; testes novos para toda regra alterada. |
| III. Sanitização em todas as superfícies | PASS | Erros canônicos na fronteira do adapter; chaves Python-side; nenhuma key em repr/log/resposta/bridge. |
| IV. Local-first no desktop | PASS | Adapter lazy; sem provider em idle; sem regressão de bridge allowlist/atomicidade/replay; smoke headless na validação. |
| V. Falha explícita, sem fallback silencioso | PASS | Seleção explícita; sem auto-routing/fallback; provider falhou = turno falhou; desconhecido falha sanitizado. |
| VI. Escopo mínimo e verificável | PASS | Uma branch, uma PR, só #337; sem dependência nova; sem refatoração oportunista; itens fora de escopo documentados. |

Nenhuma violação a justificar.

## Project Structure

### Documentation (this feature)

```text
specs/002-language-model-contract/
├── plan.md              # Este arquivo
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output (contrato LanguageModel)
└── tasks.md             # Phase 2 output (/speckit.tasks)
```

### Source Code (repository root)

```text
backend/
├── language_model.py          # NOVO: contrato canônico (LanguageModel, ModelRequest/Result, ModelFailure, ModelSelection) + trusted_policy builder do core
├── groq_language_model.py     # NOVO: adapter Groq (implementa o contrato; única morada de símbolos Groq fora de composição/testes)
├── groq_manager.py            # INALTERADO: pool/retries/classificação continuam aqui, consumidos pelo adapter
├── engine.py                  # AJUSTE: fala com LanguageModel injetado; sem imports Groq
├── process_turn.py            # AJUSTE: usa o contrato único (remove ProviderPort duplicado)
├── companion_runtime.py       # AJUSTE: usa o contrato único (remove ProviderPort duplicado); factory constrói o adapter lazy
├── main.py                    # AJUSTE: mapeia erros canônicos do contrato (sem Groq exceptions)
├── dependencies.py            # AJUSTE: composition root web constrói o adapter Groq
├── desktop/                   # INALTERADO (shell puro; bridge allowlist)
└── tests/
    ├── test_language_model.py        # NOVO: contrato + fake + política confiável no core
    ├── test_groq_language_model.py    # NOVO: adapter (SDK mockado, sem rede)
    ├── test_language_model_isolation.py  # NOVO: AST estrutural (Groq só no adapter/composição)
    └── test_companion_runtime.py / test_process_turn.py / test_emotional_integration.py / test_bounded_turn_execution.py / test_health.py  # AJUSTES: costura nova
```

**Structure Decision**: Mantida a estrutura flat do `backend/` (módulos de domínio na raiz, `desktop/` como shell). O contrato e o adapter ganham módulos dedicados (`language_model.py`, `groq_language_model.py`) para que a fronteira seja auditável por arquivo, exigência do teste estrutural e da auditoria da PR.

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

Sem violações.

## Research & Planning Details

### Phase 0 Research Summary

Ver "research.md". Pesquisas concluídas durante o levantamento do código real (sem agentes externos necessários; todo o contexto é local):

1. **Call sites reais do provider** (derivação do contrato): `ConversationEngine._appraise` (mensagens [system,user], model `fast_model_id`, temperature 0, max_tokens appraisal, `response_format={"type":"json_object"}`, valida envelope antes, parse `parse_llm_appraisal`, erros: JSON inválido→`provider_invalid_response`), `ConversationEngine._generate_with_messages` (mensagens completas, `main_model_id`, temperature 0.8, max_tokens main), `CompanionRuntime.GroqRuntimeProvider` (espelha os mesmos dois call sites com o mesmo contrato de budget/TurnBudget e estágio appraisal/generation). O contrato precisa cobrir exatamente: appraise(message, budget), generate(messages, budget), identificação provider/modelo sanitizada, e erros tipados. `build_trusted_policy` está no provider hoje, mas é responsabilidade do núcleo (política de sistema com estado emocional/relacional tipado) — sai do contrato, vai para o core.

2. **Taxonomia de erros existente**: `ProviderFailure` (rate_limited, auth_failed, connection_failed, server_error, invalid_response, invalid_request, timeout, cancelled) + `provider_failure_to_turn_code` já existem em `groq_manager.py` e são provider-agnostic no nome/vocabulário. `GroqPoolExhaustedError` (com `failure_code`) e `GroqRequestError` são os veículos atuais. Decisão: o contrato define `ModelFailure` (códigos canônicos idênticos à taxonomia existente) e `LanguageModelError`/exceções canônicas que carregam o `ModelFailure`; o adapter Groq traduz `GroqPoolExhaustedError`/`GroqRequestError`/SDK exceptions → exceções canônicas na fronteira, preservando o mapeamento existente para `TurnErrorCode` (que permanece a moeda de HTTP). `classify_provider_error` e o pool continuam em `groq_manager.py` (inalterado, privado ao adapter).

3. **Veios de Groq fora do domínio a remover**: `engine.py` (import direto de GroqClientManager + exceções + construção no `__init__`), `process_turn.py` (ProviderPort local), `companion_runtime.py` (ProviderPort local + `GroqRuntimeProvider` + `build_groq_runtime_provider`), `main.py` (tratamento de GroqPoolExhaustedError/GroqRequestError + import de provider_failure_to_turn_code), `meta_cognition.py`/`turing_test.py`/`test_keys.py` (módulos legados não ativos — fora de escopo, documentados). `health.py` `ProviderCheck` consome `engine.groq_manager.is_configured` — passa a consumir a façade do contrato no engine (`is_configured` via adapter/config da engine), mantendo o comportamento.

4. **Composição atual**: web = `dependencies.build_default_dependencies` constrói `ChatConversationEngine(groq_keys=...)` que constrói `GroqClientManager` no `__init__` (eager). Desktop = `CompanionRuntime._provider_port` lazy via `build_groq_runtime_provider` + `provider_configured_probe` via `groq_keys`. Decisão de seleção explícita: `ProviderConfig`/`settings.provider_keys()` existentes continuam sendo a fonte de chaves; a seleção provider/modelo passa a ser um `ModelSelection` explícito (provider `"groq"` + ids de modelos existentes do `ProviderConfig`) resolvido na composição; engine recebe `language_model` injetável (default construído na composição web, factory lazy no desktop). `engine.groq_manager` deixa de existir; testes que o mockavam passam a mockar o contrato ou injetar fake.

5. **Testes existentes que mockam `engine.groq_manager`** (`test_emotional_integration`, `test_archival_memory_integration`, `test_bounded_turn_execution`, `test_health`, `test_app_factory`, etc.): trocar o ponto de mock pelo seam do contrato (injetar fake `LanguageModel` ou mockar o método canônico do engine). Contagem baseline registrada (2816 passed CI-equivalente). Suítes desktop (`test_companion_runtime` 31 passed) e isolamento (`test_desktop_import_isolation`, `test_import_safety`, `test_runtime_containment`) preservam asserções de não-import.

### Phase 1 Design Artifacts

Ver `data-model.md` (entidades: LanguageModel, ModelRequest/Result, ModelFailure, ModelSelection, TrustedPolicyBuilder), `contracts/language-model-contract.md` (contrato assinaturas + invariantes + erros + lifecycle lazy), `quickstart.md` (como injetar um fake/adapter em testes e nos dois fluxos).

Design final (decisões):

- **Localização**: `backend/language_model.py` (contrato + tipos canônicos + `build_trusted_policy` do core) e `backend/groq_language_model.py` (adapter). Ambos flat no backend/, consistente com o layout atual.
- **Contrato**: `class LanguageModel(Protocol)` com `async def appraise(message: str, budget: TurnBudget) -> AppraisalV1`, `async def generate(messages: list[dict], budget: TurnBudget) -> str`, e `def describe() -> ModelSelection` (provider/model ids sanitizados para observabilidade). TurnBudget (deadline/timeout/cancelamento existente) continua sendo o portador de deadline — o contrato não inventa um segundo mecanismo de timeout.
- **Erros canônicos**: `LanguageModelUnavailable(ModelFailure, ...)`, `LanguageModelRateLimited`, `LanguageModelInvalidResponse`, `LanguageModelInvalidRequest`, `LanguageModelTimeout`, `LanguageModelCancelled`, `LanguageModelAuthFailed`, `LanguageModelConfigurationError` — todas carregando `ModelFailure` (códigos idênticos à taxonomia atual) e mensagem constante sanitizada. Mapeamento preservado: `language_failure_to_turn_code` espelha `provider_failure_to_turn_code` atual; `main.py` deixa de importar Groq.
- **Trusted policy no core**: `build_trusted_policy(emotional_state, relationship, adaptation_strategy, presentation)` (ou função pura que recebe os labels prontos) em `language_model.py` usando o template/BOUNDARY_RULE existentes; engine e runtime desktop usam a função do core; o contrato de provider perde o método.
- **Adapter Groq**: `GroqLanguageModel(manager, provider_config)` implementa o contrato; `__init__` recebe o manager já construído (composição) — o manager continua request-scoped/pool como hoje; tradução GroqPoolExhaustedError/GroqRequestError→exceções canônicas acontece nos métodos do adapter; `describe()` retorna `ModelSelection(provider="groq", model=...)`. Factory `build_groq_language_model(keys, params)` para composição web; desktop mantém factory lazy própria chamando-a.
- **Engine**: `ConversationEngine.__init__` recebe `language_model: LanguageModel | None` (+ `language_model_factory` para lazy, espelhando o padrão desktop) e expõe `is_provider_configured` para o health check; removidos todos os símbolos Groq. `ChatConversationEngine` repassa.
- **Sem auto-routing/fallback**: nenhuma lógica de escolha; `ModelSelection` único resolvido na composição; desconhecido → `LanguageModelConfigurationError` sanitizado na resolução.
- **Compat**: `ProcessTurn`/`build_process_turn` trocam `provider=engine` para o contrato único (engine continua implementando-o). Bridge/LocalStorage/allowlist inalterados. `health.ProviderCheck` ajustado para a nova façade.
- **Testes (24 categorias mapeadas em tasks)**: ver tasks.md.
