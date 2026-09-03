# Research: Decouple LanguageModel

**Feature**: specs/002-language-model-contract | **Date**: 2026-09-03

Pesquisa realizada diretamente no código real do repo (branch `refactor/language-model-contract` @ 992fa7c = origin/main). Nenhum NEEDS CLARIFICATION permanece.

## 1. Call sites reais do provider (fonte do contrato)

### Web (`backend/engine.py` — ConversationEngine)

- `_appraise(message, budget)`:
  - mensagens = `[{"role":"system","content": appraisal_policy}, {"role":"user","content": message}]`
  - `validate_provider_input(messages)` ANTES de qualquer chamada (falha local → `TurnExecutionError(provider_invalid_request)`)
  - `groq_manager.chat_completion_async(messages=..., model=provider_config.fast_model_id, budget, stage="appraisal", temperature=0, max_tokens=provider_config.appraisal_max_output_tokens, response_format={"type":"json_object"})`
  - resposta: `response.choices[0].message.content` → `json.loads` → `parse_llm_appraisal` (fallback → `provider_invalid_response`)
- `_generate_with_messages(messages, budget)`:
  - `validate_provider_input(messages)` antes; `model=provider_config.main_model_id`, `temperature=0.8`, `max_tokens=main_max_output_tokens`
  - conteúdo vazio/ausente → `provider_invalid_response`
- `provider_config` = `ProviderConfig()` de `backend/provider_models.py` (57 LOC, env-driven, ids de modelo + limites de tokens) — permanece a fonte da seleção de modelo.
- `run_archival_extraction` também chama `groq_manager.chat_completion_async` (fatos de longo prazo) — terceiro call site menor com o mesmo shape de geração.

### Desktop (`backend/companion_runtime.py` — GroqRuntimeProvider)

- Espelha exatamente os dois call sites acima (mesmos models/temperaturas/limites/JSON mode), com o mesmo `TurnBudget`.
- `build_trusted_policy` no provider usa `AffectiveEngine` (presentation) + `compute_bond_label` + template `_TRUSTED_POLICY_TEMPLATE` + `BOUNDARY_RULE` — lógica 100% do domínio Katherine, não do provider.
- Factory `build_groq_runtime_provider(keys, groq_params)` — lazy, import de `GroqClientManager` dentro da função.
- `provider_configured_probe` → `groq_keys.get_groq_api_keys()` presença apenas (nunca ecoa valor).

### Conclusão (shape do contrato)

O contrato precisa de exatamente: `appraise(message, budget) -> AppraisalV1`, `generate(messages, budget) -> str`, identificação sanitizada `describe() -> ModelSelection`. `build_trusted_policy` NÃO faz parte do contrato de modelo (é política do núcleo). TurnBudget já carrega deadline/timeout/cancelamento — não criar mecanismo novo.

## 2. Taxonomia de erros e mapeamento preservado

`backend/groq_manager.py` já possui taxonomia provider-agnostic no vocabulário:

- `ProviderFailure` (rate_limited, auth_failed, connection_failed, server_error, invalid_response, invalid_request, timeout, cancelled)
- `provider_failure_to_turn_code` → TurnErrorCode (rate_limited→upstream_rate_limited; auth_failed/invalid_request→provider_invalid_request; connection_failed/server_error→provider_unavailable; timeout→turn_timeout; invalid_response→provider_invalid_response; cancelled→internal_error propagado)
- `classify_provider_error` mapeia exceções do SDK Groq (RateLimitError, AuthenticationError, APITimeoutError, APIConnectionError, APIStatusError 401/5xx/4xx, CancelledError, TimeoutError) — sem ler str(exc), sem log.

Veículos atuais que vazam Groq: `GroqPoolExhaustedError` (carrega `failure_code`), `GroqRequestError`, `GroqConfigurationError`. `main.py._map_turn_error` os trata diretamente (import de GroqPoolExhaustedError/GroqRequestError/provider_failure_to_turn_code de groq_manager). `engine._run_under_lock` trata GroqPoolExhaustedError/GroqRequestError nos stages.

Decisão: exceções canônicas no contrato (`LanguageModel*Error` carregando `ModelFailure` + mensagens constantes) com mapeamento `language_failure_to_turn_code` idêntico ao atual. O adapter traduz na fronteira. O mapeamento HTTP em `main.py` passa a consumir só exceções canônicas/TurnExecutionError — preserva status codes (429/503/504/500) bit a bit.

## 3. Inventory de símbolos Groq acima do adapter (a eliminar)

| Arquivo | Hoje | Depois |
|---|---|---|
| `engine.py` | import GroqClientManager+3 exceções+ProviderFailure+provider_failure_to_turn_code; constrói GroqClientManager no __init__; trata Groq exceptions nos stages | importa só `language_model` (contrato); recebe LanguageModel injetado/factory; trata exceções canônicas |
| `process_turn.py` | `ProviderPort` local (Protocol com appraise/generate/build_trusted_policy) | usa contrato único; trusted policy via core |
| `companion_runtime.py` | `ProviderPort` local, `GroqRuntimeProvider`, `build_groq_runtime_provider`, import lazy de GroqClientManager + Groq exceptions | usa contrato único; adapter Groq externo; runtime recebe factory do LanguageModel |
| `main.py` | GroqPoolExhaustedError/GroqRequestError no except + provider_failure_to_turn_code importado | só exceções canônicas (TurnExecutionError/LanguageModel*Error) |
| `dependencies.py` | passa groq_keys para ChatConversationEngine | composition root constrói GroqLanguageModel e injeta |
| `health.py` | ProviderCheck(engine.groq_manager) | ProviderCheck consome façade `engine.provider_status()`/is_configured do contrato |
| `meta_cognition.py`, `turing_test.py`, `test_keys.py` | importam GroqClientManager/groq SDK | FORA DE ESCOPO (módulos legados não ativos, não referenciados pelo fluxo) — documentar na PR |
| `final_verification.py` | usa turing_test | idem |

Testes que mockam `engine.groq_manager` (para trocar o seam): `test_emotional_integration.py` (19 refs), `test_archival_memory_integration.py` (16), `test_bounded_turn_execution.py` (83), `test_health.py` (7), `test_app_factory.py` (5), `test_import_safety.py` (10 — verificação de não-import de groq em main já existe, adaptar), `test_runtime_containment.py` (9), `test_settings.py` (14), `test_companion_runtime.py` (12), `test_provider_models.py` (40 — config pura, pouco muda).

## 4. Composição e lifecycle

- **Web**: `build_default_dependencies` (único lugar de construção real; lifespan-owned). `ChatConversationEngine(groq_keys=...)` hoje constrói GroqClientManager eager no __init__ (com factory `create_app()` sem clients até lifespan). Depois: composição constrói `GroqLanguageModel` (manager eager como hoje — sem mudança de custo) e injeta no engine. `settings.provider_keys()` continua a fonte de chaves (Python-side).
- **Desktop**: `CompanionRuntime.__init__(provider=None, provider_factory=None)` — lazy já correto. Depois: factory default = `build_groq_language_model` (novo adapter), criado no primeiro turno. `runtime_state()` usa `provider_configured_probe` (presença de chave, sem construir client) — preservado. Import de `backend.groq_language_model` fica dentro da factory (lazy), mantendo o import-time do desktop shell sem SDK.
- **Seleção explícita**: `ModelSelection(provider="groq", models=...)` resolvido na composição a partir de `ProviderConfig` existente. Nenhum mecanismo de escolha automática. Provider != "groq" → `LanguageModelConfigurationError` sanitizado (composição falha no lifespan/lazy factory falha no turno, ambos sanitizados).

## 5. Isolamento de import / smoke existentes (não regredir)

- `test_desktop_import_isolation.py` / `test_import_safety.py`: garantem `backend.desktop` shell puro e import-time barato. O adapter novo (`groq_language_model.py`) não pode ser importado por `desktop/` ou no módulo `companion_runtime` top-level (só dentro da factory).
- `test_runtime_containment.py`: contém verificações de runtime desktop.
- `scripts/desktop_smoke.py` (xvfb-run): abertura headless do app — gate de validação final.
- Baselines coletadas: suíte CI-equivalente (sem integração Supabase) = **2816 passed**; `test_companion_runtime.py` = 31 passed.

## 6. Decisões de design confirmadas pelo código

1. Contract em `backend/language_model.py`; adapter em `backend/groq_language_model.py`; `groq_manager.py` inalterado (pool/classification/retries request-scoped já testados por `test_groq_manager.py` 783 LOC).
2. Trusted policy: função `build_trusted_policy(...)` do core em `language_model.py`, usando o template canônico único (o do engine/companion — são idênticos em semântica; o template `_TRUSTED_POLICY_TEMPLATE` do companion_runtime e o inline do engine serão unificados num só lugar, reutilizando texto atual sem mudança de comportamento).
3. `TurnBudget` permanece o portador de deadline/timeout/cancelamento (create_budget/run_blocking_* existentes). Contrato não redefin e não duplica.
4. Sem `**kwargs`: assinaturas explícitas com dataclasses canônicas; mensagens = `list[dict[str, str]]` estruturadas (formato já validado por `validate_provider_input`), nunca objetos SDK.
5. Capability flags: nenhuma (YAGNI; issue explicitamente veda abstração universal).
6. Sem dependência nova; sem mudança de storage; sem tocar migrations.
