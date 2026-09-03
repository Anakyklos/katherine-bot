# Data Model: Decouple LanguageModel

**Feature**: specs/002-language-model-contract | **Date**: 2026-09-03

Nenhuma entidade persistida nova. Storage (Supabase web / SQLite desktop), migrations e payloads persistidos permanecem idênticos. As entidades abaixo são contratos de código (runtime), não dados persistidos.

## Entidades (contratos de código)

### LanguageModel (interface canônica — Protocol)

A única fronteira entre o núcleo Katherine e qualquer geração de linguagem (remota hoje; local no futuro).

```python
class LanguageModel(Protocol):
    async def appraise(self, message: str, budget: TurnBudget) -> AppraisalV1: ...
    async def generate(self, messages: list[dict[str, str]], budget: TurnBudget) -> str: ...
    def describe(self) -> ModelSelection: ...
```

Invariantes:

- Sem `**kwargs`; sem objetos de SDK em entrada ou saída.
- `messages` são estruturadas (`role`, `content`), validadas por `validate_provider_input` ANTES de qualquer chamada (preservado do comportamento atual).
- `budget` (TurnBudget existente) carrega deadline/timeout/cancelamento — o contrato não cria mecanismo paralelo.
- Implementações não guardam estado por usuário; estado por requisição, como hoje.

### ModelSelection (seleção explícita, sanitizada)

```python
@dataclass(frozen=True)
class ModelSelection:
    provider: str        # hoje: "groq"
    main_model_id: str   # id de modelo explícito (observabilidade sanitizada)
    fast_model_id: str   # id do modelo de appraisal
```

- Resolve na composição (web: `build_default_dependencies`; desktop: factory lazy).
- Provider desconhecido → `LanguageModelConfigurationError` sanitizado (não importa modelo/rota).
- Nunca contém chaves, tokens, URLs com credencial ou detalhe de SDK.

### ModelFailure (taxonomia canônica de falha)

Códigos de baixa cardinalidade, idênticos ao vocabulário existente (`ProviderFailure` de `groq_manager.py`):

```text
rate_limited | auth_failed | connection_failed | server_error |
invalid_request | invalid_response | timeout | cancelled
```

Exceções canônicas (todas em `backend/language_model.py`, mensagens constantes, sem texto de exceção bruta, chave, prompt ou conteúdo):

```python
LanguageModelError(ModelFailure, message: str constante)   # base
LanguageModelRateLimitedError      # rate_limited
LanguageModelAuthFailedError       # auth_failed
LanguageModelConnectionFailedError # connection_failed
LanguageModelServerError           # server_error
LanguageModelInvalidRequestError   # invalid_request
LanguageModelInvalidResponseError   # invalid_response
LanguageModelTimeoutError          # timeout
LanguageModelCancelledError        # cancelled
LanguageModelConfigurationError    # configuração ausente/inválida (ex.: sem chave, provider desconhecido)
```

Mapeamento preservado (função `language_failure_to_turn_code`): idêntico ao `provider_failure_to_turn_code` atual — rate_limited→upstream_rate_limited (429); auth_failed/invalid_request→provider_invalid_request (503); connection_failed/server_error→provider_unavailable (503); timeout→turn_timeout (504); invalid_response→provider_invalid_response (500); cancelled→propagado (internal_error apenas quando convertido).

### TrustedPolicyBuilder (política confiável — responsabilidade do core)

Função canônica do núcleo (em `backend/language_model.py`), extraída do contrato de provider:

```python
def build_trusted_policy(
    emotional_state: EmotionalStateV1,
    relationship: RelationshipStateV1,
    adaptation_strategy: str = "",
    presentation: AffectiveEngine | None = None,
) -> str
```

- Usa o template canônico único (texto atual preservado: estado interno, vínculo, instrução de atuação, transparência de identidade, pronomes femininos, não-manipulação/não-sexualização, limites sem escalada, regras de estilo) + `BOUNDARY_RULE`.
- Nenhum conteúdo derivado do usuário (histórico, perfil, memórias) entra aqui — preservado.
- Engine (web) e CompanionRuntime (desktop) passam a chamá-la diretamente; o provider NÃO participa.

### GroqLanguageModel (adapter — única morada de símbolos Groq)

```python
class GroqLanguageModel:
    def __init__(self, manager: GroqClientManager, provider_config: ProviderConfig): ...
    # implementa LanguageModel; traduz GroqPoolExhaustedError/GroqRequestError/SDK → exceções canônicas na fronteira
```

- `groq_manager.py` permanece inalterado (pool request-scoped, retries limitados explícitos, classificação).
- Factory `build_groq_language_model(keys, groq_params)` para composição; desktop importa a factory lazy (dentro de função, nunca no top-level do runtime).

## Fluxo de dados (inalterado nas bordas)

1. Usuário envia mensagem → engine valida/admite → carrega estado+contexto (Supabase ou SQLite).
2. Core constrói trusted policy + envelope validado (budgets/seleção de contexto existentes).
3. Core chama `LanguageModel.appraise` / `LanguageModel.generate` com TurnBudget.
4. Adapter traduz para chamadas concretas do provider selecionado (hoje Groq) e traduz falhas para exceções canônicas.
5. Falha do provider = falha do turno (TurnErrorCode canônico → HTTP/LocalErrorCode). Sem fallback, sem auto-routing, sem retry além dos limites explícitos existentes.
6. Persistência atômica/replay idêntica à atual (nenhuma mudança de payload).

## Privacidade / dados sensíveis

- Chaves: env/arquivo Python-side (loaders existentes); nunca no bundle React, bridge, `repr`, logs, erros. Sem sincronização automática.
- Quando provider remoto configurado: somente o envelope validado/limitado trafega (budgets existentes), nunca banco/histórico inteiro.
- Estado emocional/relacional e memórias permanecem no domínio (fora do adapter); isolamento por usuário preservado.
