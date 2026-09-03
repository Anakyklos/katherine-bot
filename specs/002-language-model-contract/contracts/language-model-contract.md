# Contract: LanguageModel

**Feature**: specs/002-language-model-contract | **Date**: 2026-09-03
**Localização**: `backend/language_model.py` (contrato + tipos canônicos + trusted policy do core); implementação de referência: `backend/groq_language_model.py`.

## Interface

```python
class LanguageModel(Protocol):
    """Contrato canônico de geração de linguagem da Katherine.

    Katherine Core (engine, process_turn, companion_runtime) depende apenas
    desta interface. Implementações concretas (remotas ou locais, futuras)
    são plugadas na composição. Nenhum símbolo de SDK atravessa esta
    fronteira.
    """

    async def appraise(self, message: str, budget: TurnBudget) -> AppraisalV1:
        """Avalia o impacto emocional da mensagem do usuário.

        Retorna AppraisalV1 estruturado (parse do JSON do provider).
        Levanta exceções canônicas LanguageModel*Error em falha.
        """

    async def generate(self, messages: list[dict[str, str]], budget: TurnBudget) -> str:
        """Gera resposta a partir de mensagens estruturadas validadas.

        Retorna o texto da resposta. Levanta exceções canônicas em falha.
        Conteúdo vazio/inválido → LanguageModelInvalidResponseError.
        """

    def describe(self) -> ModelSelection:
        """Identificação sanitizada para observabilidade.

        Retorna provider + ids de modelo. Nunca contém chaves, tokens,
        URLs com credencial ou detalhes de SDK.
        """
```

## Tipos canônicos

```python
@dataclass(frozen=True)
class ModelSelection:
    provider: str          # ex.: "groq" — o único provider atual
    main_model_id: str      # modelo de geração (explícito)
    fast_model_id: str      # modelo de appraisal (explícito)

class ModelFailure(str, Enum):
    rate_limited = "rate_limited"
    auth_failed = "auth_failed"
    connection_failed = "connection_failed"
    server_error = "server_error"
    invalid_request = "invalid_request"
    invalid_response = "invalid_response"
    timeout = "timeout"
    cancelled = "cancelled"
```

## Exceções canônicas

Todas herdam de `LanguageModelError` e carregam `failure: ModelFailure` + mensagem constante sanitizada. `LanguageModelConfigurationError` cobre configuração ausente/inválida (sem chave, provider desconhecido). `str(exc)` nunca contém texto de exceção bruta, chave, prompt, conteúdo do usuário, HTTP body ou detalhe de SDK.

```python
class LanguageModelError(Exception):
    def __init__(self, failure: ModelFailure, message: str): ...

# Concretas: RateLimited, AuthFailed, ConnectionFailed, ServerError,
# InvalidRequest, InvalidResponse, Timeout, Cancelled, Configuration
```

## Mapeamento para TurnErrorCode (público, preservado)

`language_failure_to_turn_code(failure) -> TurnErrorCode` — idêntico ao mapeamento atual (`provider_failure_to_turn_code`):

| ModelFailure | TurnErrorCode | HTTP |
|---|---|---|
| rate_limited | upstream_rate_limited | 429 |
| auth_failed | provider_invalid_request | 503 |
| invalid_request | provider_invalid_request | 503 |
| connection_failed | provider_unavailable | 503 |
| server_error | provider_unavailable | 503 |
| timeout | turn_timeout | 504 |
| invalid_response | provider_invalid_response | 500 |
| cancelled | (propagado; internal_error se convertido) | — |

Desktop: mesma taxonomia → LocalErrorCode sanitizado existente.

## Trusted policy (core, não do provider)

```python
def build_trusted_policy(
    emotional_state: EmotionalStateV1,
    relationship: RelationshipStateV1,
    adaptation_strategy: str = "",
    presentation: AffectiveEngine | None = None,
) -> str
```

- Política de sistema confiável construída a partir de estado emocional/relacional tipado + instruções de atuação (presentation) + `compute_bond_label` + `BOUNDARY_RULE`.
- Template canônico único (texto atual, sem mudança de comportamento).
- Sem conteúdo derivado do usuário. Sem I/O. Pura.

## Ciclo de vida e performance

- **Web**: composição (`build_default_dependencies`) constrói o adapter e injeta no engine dentro do lifespan — nenhum custo novo em idle.
- **Desktop**: factory lazy; o adapter é construído no primeiro turno que precisa dele; import do adapter somente dentro da factory (import-time do shell preservado).
- Provider ausente: web falha no lifespan (composição) sanitizado; desktop abre normalmente (local-first) e o turno retorna erro de configuração claro sem quebrar persistência.
- Sem threads/workers de provider em background; retries somente os limites explícitos existentes (groq_manager).

## Seleção explícita (sem auto-routing)

- `ModelSelection` é resolvido na composição a partir de `ProviderConfig` (ids explícitos, env-driven).
- Nenhuma lógica de escolha automática de provider/modelo em runtime.
- Provider selecionado falha → turno falha (código canônico). Nunca troca de provider.
- Provider desconhecido na resolução → `LanguageModelConfigurationError` sanitizado imediatamente.

## Invariantes de segurança (testáveis)

1. Nenhum símbolo Groq/SDK acima do adapter: `engine.py`, `process_turn.py`, `companion_runtime.py` (fora da factory), `main.py`, `desktop/*` não importam `groq_manager`, `groq` SDK, nem exceções Groq. Teste AST garante; adapter, composição e testes do adapter são a allowlist.
2. Chaves somente Python-side: nenhum valor/prefixo de chave em logs, `repr`, exceções `str()`, respostas HTTP/bridge, bundle React.
3. Envelope validado antes de qualquer chamada ao provider (comportamento atual preservado).
4. Erros canônicos atravessam fronteiras com mensagens constantes; texto bruto nunca.
5. Sem estado de usuário no contrato/adapter (por requisição, como hoje).
6. `validate_provider_input` continua a gate de input; budgets/seleção de contexto inalterados (só envelope necessário trafega ao provider remoto).

## Exemplos de uso

### Fake determinístico (testes de domínio, sem rede)

```python
class FakeLanguageModel:
    def __init__(self): self.calls = []
    async def appraise(self, message, budget):
        self.calls.append(("appraise", message))
        return AppraisalV1(...)
    async def generate(self, messages, budget):
        self.calls.append(("generate", [m["role"] for m in messages]))
        return "resposta determinística"
    def describe(self):
        return ModelSelection(provider="fake", main_model_id="fake-main", fast_model_id="fake-fast")
```

### Composição web

```python
model = GroqLanguageModel(GroqClientManager(keys=settings.provider_keys(),
                                            groq_params=turn_config.to_groq_params()),
                          provider_config)
engine = ChatConversationEngine(language_model=model, ...)
```

### Factory lazy desktop

```python
def _default_language_model_factory():
    return build_groq_language_model()  # import dentro da função

runtime = CompanionRuntime(storage_path=..., language_model_factory=_default_language_model_factory)
```
