# Application Lifecycle

Status: implementado na issue #275.

Este documento descreve como o backend é composto, iniciado e encerrado:
settings validados, container de dependências, app factory, lifespan e
ownership dos recursos.

## Problema e causa raiz

O backend anterior construía dependências no carregamento dos módulos:

```text
import backend.main
→ cria engine (cliente Groq + MemoryManager + SentenceTransformer + Supabase)
→ lê CORS, archival flag e timeouts de ambiente no import
→ registra /health sem checar dependências
```

Consequências:

- efeitos colaterais durante import (rede, modelos, clientes);
- testes dependentes de monkeypatch em `sys.modules` e de globais de módulo;
- impossibilidade de fechar clientes no shutdown;
- configuração de produção baseada em defaults de desenvolvimento;
- health check incapaz de distinguir processo vivo de instância apta;
- baixa capacidade de injetar doubles nos testes.

## Solução

A composição foi separada da lógica de negócio em quatro camadas:

1. `backend/settings.py` — configuração tipada e validada (ver
   [Configuration](operations/configuration.md)).
2. `backend/dependencies.py` — container explícito
   `ApplicationDependencies` + builder padrão + ownership.
3. `backend/main.py` — `create_app(settings, dependencies)` + lifespan.
4. `backend/health.py` — checks de readiness (ver
   [Health and Readiness](operations/health-and-readiness.md)).

## Contrato da app factory

```python
def create_app(
    settings: Settings | None = None,
    dependencies: ApplicationDependencies | None = None,
) -> FastAPI: ...
```

- `settings=None` → `Settings.from_env()` (validação estrita, falha cedo).
- `dependencies=None` → o lifespan constrói a composição padrão
  (`build_default_dependencies(settings)`) e a aplicação é dona dos recursos.
- `dependencies=<container>` → o chamador mantém ownership; a aplicação não
  constrói nada no startup e marca o ciclo como concluído imediatamente.
- A factory **nunca** constrói cliente Groq, cliente Supabase, modelo de
  embeddings, threads ou tarefas em background. Importar `backend.main` é
  seguro e sem efeitos colaterais.
- O container fica em `app.state.dependencies`; endpoints o recuperam por
  dependency functions pequenas (`get_dependencies(request)`), nunca por
  globais.

## Container de dependências

```python
@dataclass
class ApplicationDependencies:
    conversation_engine: ConversationEngine  # ProcessTurn + provider + persistência
    auth_client: object                      # superfície de auth (cliente Supabase)
    admission_config: AdmissionRuntimeConfig # ledger de admissão + HMAC
    turn_config: TurnExecutionConfig         # budget/deadline do turno
    health_checks: HealthRegistry            # checks de readiness
    clock: Callable[[], float]               # relógio de parede
    persistence_client: object               # superfície de persistência (history/admission)
```

- Nenhum estado por usuário vive no container ou em singleton: identidade
  autenticada é resolvida por request (`get_current_user`), e snapshots
  emocionais/relacionais permanecem no domínio/persistência.
- `auth_client` e `persistence_client` são superfícies explícitas: as rotas
  usam somente a dependência correta (`auth_client` para autenticação,
  `persistence_client` para history/admission) e nunca navegam por
  `conversation_engine.memory_manager`. Fakes distintos podem ser injetados
  para provar o isolamento.
- `UserLockManager` permanece process-wide porque já é thread-safe por
  construção e é um recurso de infraestrutura, não estado de usuário.
- Para adicionar uma dependência nova, ver “Procedimento para adicionar uma
  dependência” abaixo.

## Ordem de startup (lifespan)

```text
1. Validar configuração final (Settings congelado; re-validação completa).
2. Se não injetado: build_default_dependencies(settings)
   a. engine (GroqClientManager + MemoryManager + lock manager); o modelo de
      embeddings só é construído quando EMBEDDINGS_RETRIEVAL_ENABLED=true
   b. admission_config a partir dos settings
   c. probes de readiness (auth e database) construídos a partir dos settings:
      I/O HTTP assíncrono bounded (httpx), sem threads, sem cliente owned
   d. health registry (configuration, auth, database, persistence,
      provider[, embeddings])
   e. container concluído
3. Falha em qualquer passo:
   a. recursos já criados pelo builder são fechados (cleanup parcial)
   b. app.state.owned_resources é drenado
   c. evento sanitizado `event=app_startup_failed`
   d. a exceção propaga: a aplicação não começa a servir tráfego
4. Somente após sucesso: `app.state.lifespan_started = True`
```

## Ordem de shutdown (lifespan)

```text
1. Marcar lifespan como não iniciado (impede readiness durante shutdown e
   faz get_dependencies() recusar novas requisições).
2. Fechar cada recurso owned:
   a. contrato: aclose() → close() (o primeiro disponível)
   b. falha em um recurso não interrompe os demais
   c. apenas `event=app_shutdown_failed` é registrado (código sanitizado)
3. Limpar app.state.owned_resources.
4. Se a composição era owned (construída pela aplicação), limpar
   app.state.dependencies: o próximo ciclo de lifespan constrói uma
   composição nova com recursos novos, nunca reutiliza recursos já fechados.
5. Recursos injetados externamente NUNCA são fechados pela aplicação e
   permanecem em app.state para o chamador; as rotas ainda recusam operar
   fora do lifespan (get_dependencies() exige lifespan_started).
```

## Ciclos de lifespan

- Composição **owned**: cada entrada no lifespan executa
  `build_default_dependencies()`; o shutdown fecha e descarta a composição.
  Um segundo ciclo constrói recursos novos (testado com fakes e com o builder
  padrão).
- Composição **injetada**: o chamador mantém ownership; o shutdown não fecha
  nem descarta, mas as rotas continuam recusando operar fora do lifespan.

## Ownership

| Recurso | Criado por | Fechado por |
| --- | --- | --- |
| Engine/managers (default) | `build_default_dependencies` | aplicação (shutdown; hoje sem contrato close/aclose no grafo) |
| Registry de readiness (checks) | `build_default_dependencies` | aplicação (shutdown e startup parcial) |
| Probes de readiness (auth/database) | `build_default_dependencies` (callables async) | sem recurso owned (I/O HTTP assíncrono; `aclose()` cancela probes em voo) |
| Cliente Supabase (default) | `build_default_dependencies` via factory dos settings | aplicação (se expuser close/aclose) |
| Container injetado | chamador (teste/deploy) | chamador |
| Clientes Groq por request | `GroqClientManager` | request-scoped (fechados por call) |

O builder padrão retorna `owned = (health_checks,)`. O registry de readiness é
o único recurso owned: `HealthRegistry.aclose()` prefere o `aclose` de cada
check; `AuthClientCheck.aclose()` e `DatabaseCheck.aclose()` **cancelam
qualquer probe em voo e aguardam sua terminação antes de retornar** — os
probes são I/O HTTP assíncrono (httpx), então o cancelamento realmente para a
operação. Não há threads, executors nem clientes de probe owned; nenhum
trabalho de readiness sobrevive ao lifespan e nenhum cleanup fire-and-forget
é necessário. `HealthRegistry.close()` (síncrono, usado em falha parcial de
startup) apenas rejeita novos probes. A versão atual do SDK Supabase pinado
não expõe `close()`/`aclose()`, e os clientes Groq são request-scoped, então o
grafo do engine não entra na tupla owned hoje.

O readiness também valida as **superfícies reais** usadas pelas rotas: os
checks `auth` e `persistence` verificam `auth_client` e `persistence_client`
(criados com o engine). Um probe saudável nunca substitui essas superfícies:
se a criação do cliente real falhar, `/ready` responde 503 mesmo com o probe
funcionando.

## Importabilidade

Importar `backend.main`, `backend.engine`, `backend.memory`,
`backend.groq_manager`, `backend.dependencies`, `backend.settings`,
`backend.health`, `backend.observability` e `backend.process_turn`:

- não abre socket;
- não constrói cliente Groq ou Supabase;
- não instancia `SentenceTransformer`;
- não inicia thread nem background task;
- não lê arquivos de runtime nem executa migration;
- não imprime nada.

O único acesso a ambiente no import é `Settings.from_env()` na construção do
app de módulo (`app = create_app()`), que é validação pura. Como `APP_ENV` é
obrigatório (fail-closed), o runtime (Docker/CI/process manager) precisa
declará-lo; sem ele, `create_app()` falha com erro sanitizado em vez de rodar
em modo implícito.

## Procedimento para adicionar uma dependência nova

1. Adicione o campo ao container `ApplicationDependencies` (sem estado por
   usuário).
2. Construa o recurso em `build_default_dependencies` e, se a aplicação deve
   fechá-lo, inclua-o na tupla de `owned_resources` e implemente
   `close()`/`aclose()`.
3. Se for crítico para tráfego nominal, adicione um check em `health.py` com
   timeout explícito e registre-o em `build_health_registry`.
4. Se exigir configuração, adicione o campo em `Settings` com validação
   estrita e documente a variável em `docs/operations/configuration.md`.
5. Teste: factory com fakes injetados, lifespan, ownership, readiness.
