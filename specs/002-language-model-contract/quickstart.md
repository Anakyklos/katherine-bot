# Quickstart: Decouple LanguageModel

**Feature**: specs/002-language-model-contract | **Date**: 2026-09-03

## Como usar o contrato (visão de 2 minutos)

### 1. Domínio consome só a interface

```python
from backend.language_model import LanguageModel, build_trusted_policy

class MeuFluxo:
    def __init__(self, model: LanguageModel):
        self._model = model   # injetado; nunca construído aqui

    async def turno(self, mensagem: str, budget: TurnBudget) -> str:
        appraisal = await self._model.appraise(mensagem, budget)
        policy = build_trusted_policy(estado, relacionamento)  # core, não provider
        mensagens = [{"role": "system", "content": policy}, ...]
        return await self._model.generate(mensagens, budget)
```

### 2. Adapter real (Groq) — único lugar com SDK

```python
from backend.groq_language_model import GroqLanguageModel, build_groq_language_model

model = build_groq_language_model(keys=..., groq_params=...)
model.describe()  # ModelSelection(provider="groq", main_model_id=..., fast_model_id=...)
```

Erros do SDK viram exceções canônicas (`LanguageModelRateLimitedError`, `LanguageModelTimeoutError`, ...) na fronteira do adapter — o chamador nunca vê `Groq*Error`.

### 3. Testes: fake determinístico, sem rede/SDK

```python
from backend.language_model import LanguageModel, ModelSelection

class FakeLanguageModel:
    def __init__(self, resposta="ok", appraisal=None):
        self.resposta = resposta
        self.appraisal_fixo = appraisal
        self.chamadas = []
        self.falha = None

    async def appraise(self, message, budget):
        self.chamadas.append(("appraise", message))
        if self.falha: raise self.falha
        return self.appraisal_fixo or AppraisalV1(...)

    async def generate(self, messages, budget):
        self.chamadas.append(("generate", len(messages)))
        if self.falha: raise self.falha
        return self.resposta

    def describe(self):
        return ModelSelection(provider="fake", main_model_id="m", fast_model_id="f")

# engine = ConversationEngine(language_model=FakeLanguageModel())  — sem rede, determinístico
```

### 4. Composição

- **Web**: `build_default_dependencies` (backend/dependencies.py) constrói `GroqLanguageModel` e injeta em `ChatConversationEngine(language_model=...)`.
- **Desktop**: `CompanionRuntime(language_model_factory=...)` — lazy, criado no primeiro turno. Sem factory e sem chaves → app abre normal (local-first); turno sem provider → erro de configuração sanitizado.

## Comandos de validação

```bash
# compile
python -m compileall -q backend

# suíte CI-equivalente (sem integração Supabase)
python -m pytest backend/tests --ignore=backend/tests/test_database_authorization_integration.py \
  --ignore=backend/tests/test_admission_ledger_integration.py \
  --ignore=backend/tests/test_legacy_upgrade.py \
  --ignore=backend/tests/test_transactional_schema_legacy.py \
  --ignore=backend/tests/test_transactional_schema_integration.py \
  --ignore=backend/tests/test_atomic_turn_commit_integration.py \
  --ignore=backend/tests/test_process_turn_integration.py \
  --ignore=backend/tests/test_rls_auto_enable_legacy.py \
  --ignore=backend/tests/test_privacy_operations_integration.py \
  --ignore=backend/tests/test_privacy_operations_legacy.py \
  --ignore=backend/tests/test_privacy_operations_preflight.py \
  --ignore=backend/tests/test_privacy_api_integration.py \
  --ignore=backend/tests/test_retention_integration.py \
  --ignore=backend/tests/test_retention_preflight.py \
  --ignore=backend/tests/test_account_deletion_integration.py \
  --ignore=backend/tests/test_account_deletion_legacy.py \
  --ignore=backend/tests/test_account_deletion_preflight.py -q -ra

# suítes específicas do contrato
python -m pytest backend/tests/test_language_model.py backend/tests/test_groq_language_model.py \
  backend/tests/test_language_model_isolation.py backend/tests/test_companion_runtime.py \
  backend/tests/test_process_turn.py backend/tests/test_emotional_integration.py -q

# smoke desktop (headless)
xvfb-run -a python scripts/desktop_smoke.py

# frontend (não deve mudar funcionalmente)
npm test && npm run lint && npm run build
```

## Auditoria pós-implementação (checklist da PR)

```bash
# Símbolos Groq só no adapter/composição/testes do adapter/docs
grep -rn "GroqClientManager\|GroqRequestError\|GroqPoolExhaustedError\|ProviderFailure\|from groq import\|import groq" backend/ --include="*.py" | grep -v "backend/groq_manager.py\|backend/groq_language_model.py\|tests/"
```
