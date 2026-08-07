# Health and Readiness

Status: implementado na issue #275.

Este documento define a semântica de `/live`, `/ready` e `/health`, os checks
críticos e opcionais, os timeouts e o procedimento de diagnóstico.

## Diferença entre `/live`, `/ready` e `/health`

| Endpoint | Semântica | Falha de dependência? | Uso |
| --- | --- | --- | --- |
| `GET /live` | Processo e event loop respondem | Ignorada | Liveness de orquestradores |
| `GET /ready` | Instância apta a receber tráfego nominal | 503 | Readiness de orquestradores |
| `GET /health` | Alias legado de processo vivo | Ignorada | Compatibilidade (consumidores antigos) |

### `GET /live`

Resposta fixa e estável:

```json
{"status": "live"}
```

Nunca chama provider, banco, embeddings ou checks. Não inclui versões,
hostname, configuração ou detalhes internos. Retorna 200 enquanto o processo
responder.

### `GET /ready`

Executa os checks críticos com timeout explícito por check. Resposta:

```json
{"status": "ready", "components": {"configuration": "ok", "database": "ok", "provider": "ok", "lifespan": "ok"}}
```

Quando qualquer componente crítico falha:

```json
{"status": "not_ready", "components": {"configuration": "ok", "database": "unavailable", "provider": "ok", "lifespan": "ok"}}
```

com HTTP 503. A resposta nunca inclui URLs, chaves, nomes de projeto, texto
de exceção, contagens ou IDs de usuário. A ordem dos componentes é
determinística.

### `GET /health`

Mantido por compatibilidade (consumidores legados e CI de deploy). Retorna
exatamente `{"status": "alive"}` e **não afirma readiness**: nenhum check de
banco/provider é executado. Consumidores novos devem usar `/live` e `/ready`.

## Checks críticos e opcionais

Ordem determinística registrada em `backend/health.py`:

| Componente | Crítico | O que verifica | Timeout padrão |
| --- | --- | --- | --- |
| `configuration` | Sim | Settings ainda válidos (re-validação completa do modelo congelado, sem I/O) | 1s |
| `auth` | Sim | A superfície real de autenticação usada pelas rotas é efetivamente chamável (`auth_client.auth.get_user`, nunca invocada) **e** o serviço de Auth está disponível: probe HTTP assíncrono bounded de `{supabase_url}/auth/v1/health` (GoTrue), com timeout de transporte alinhado a `READINESS_AUTH_TIMEOUT_MS`, sem token de usuário e sem ler dados de usuário (só o status HTTP é observado; o corpo é descartado). Sendo I/O assíncrono, o timeout de readiness realmente cancela o probe | `READINESS_AUTH_TIMEOUT_MS` (1000) |
| `database` | Sim | Acesso mínimo ao Supabase: probe HTTP assíncrono bounded de `{supabase_url}/rest/v1/profiles?select=user_id&limit=1` (PostgREST), com timeout de transporte alinhado a `READINESS_DATABASE_TIMEOUT_MS`. Sendo I/O assíncrono, o timeout de readiness realmente cancela o probe | `READINESS_DATABASE_TIMEOUT_MS` (3000) |
| `persistence` | Sim | A superfície real de persistência usada por admissão/histórico (`persistence_client`) é efetivamente chamável: `table(...)` e `rpc(...)` existem, são callable e são invocadas com argumentos benignos (construção pura de request, sem rede) | 1s |
| `provider` | Sim | Caminho do provider configurado: `GroqClientManager.is_configured()` (chaves válidas, sem geração) | `READINESS_PROVIDER_TIMEOUT_MS` (1000) |
| `embeddings` | Somente com `EMBEDDINGS_RETRIEVAL_ENABLED=true` | Modelo de embeddings carregado no startup (atributo, sem carregar modelo) | 1s |
| `lifespan` | Sim | `app.state.lifespan_started` (startup concluído) | 1s |

Políticas:

- **O cliente de probe nunca substitui as superfícies reais.** Os checks `auth`
  e `persistence` validam os clientes que as rotas realmente usam
  (`auth_client`/`persistence_client`); se a criação do cliente do engine
  falhar (ou retornar `None`) enquanto o probe dedicado funciona, `/ready`
  responde 503: uma instância incapaz de autenticar ou persistir não é ready,
  por mais saudável que o probe esteja.
- **O componente `auth` prova disponibilidade real, não só contrato local.**
  Além da superfície chamável, um probe de rede bounded consulta o `/health`
  do GoTrue: PostgREST saudável não mascara uma queda do serviço de Auth.
  Uma instância cuja autenticação em `/chat`/`/history` falharia responde
  `auth=unavailable` no `/ready` (testado com serviço Auth indisponível + banco
  saudável → 503).
- **O check de provider nunca executa geração real.** É barato, limitado e
  verifica configuração do caminho. Deployments que quiserem um probe real de
  rede devem injetar um check próprio com timeout documentado.
- **O check de banco não abandona trabalho.** O probe é I/O HTTP assíncrono (httpx) com timeout de transporte alinhado ao `READINESS_DATABASE_TIMEOUT_MS`; o `asyncio.wait_for` do registry realmente cancela a operação, então não existem threads de worker, acumulação de polling nem trabalho órfão após timeout. Enquanto um probe está em voo, novos polls falham rápido (guarda de probe único), sem duplicar requests.
- **O registry de readiness é owned pela aplicação.** O builder padrão inclui `health_checks` na tupla de `owned_resources`. No shutdown assíncrono, `HealthRegistry.aclose()` chama o `aclose()` de cada check, que **cancela qualquer probe em voo e aguarda sua terminação antes de retornar** — nenhum trabalho owned sobrevive ao lifespan, sem cleanup fire-and-forget e sem ownership perdido. Em falha parcial de startup, `close()` síncrono apenas rejeita novos probes (não há threads nem clientes owned a liberar).
- **A guarda de probe é atômica no event loop.** O clear do estado só
  acontece quando a task observada é a que o poll dono aguarda; uma task
  mais nova instalada por um poll concorrente nunca é limpa por engano, então
  no máximo um probe fica em voo (testado com concorrência determinística).
- **Feature opcional desligada não bloqueia readiness** (`embeddings` só
  existe quando `EMBEDDINGS_RETRIEVAL_ENABLED=true`), e o modelo nunca é
  construído quando a feature está desligada.
- **Feature obrigatória habilitada e indisponível bloqueia readiness.** Com
  a recuperação vetorial habilitada, um modelo ausente torna a instância
  `not_ready` (o caminho do turno também falha de forma honesta, nunca retorna
  vazio silenciosamente). No modo habilitado, falha de encode, transporte/RPC
  ou resposta estruturalmente inválida produz `ContextLoadError` sanitizado;
  somente uma resposta válida com `data=[]` representa ausência real de
  memórias.
- Check lento é interrompido pelo timeout aprovado (nunca fica pendurado).
- Exceções com conteúdo sensível nunca aparecem na resposta nem nos logs:
  o registry emite apenas `event=readiness_check_failed component=<nome>`.

## Timeouts de readiness

- `READINESS_DATABASE_TIMEOUT_MS`: faixa 100–30000, default 3000.
- `READINESS_PROVIDER_TIMEOUT_MS`: faixa 100–30000, default 1000.
- `READINESS_AUTH_TIMEOUT_MS`: faixa 100–30000, default 1000 (probe `/health`).
- Checks internos (configuration, embeddings, lifespan): 1s fixo.
- Valores inválidos são rejeitados pelos settings (falha cedo).

## Diagnóstico quando `/ready` falhar

1. **`lifespan` unavailable** — a aplicação não completou o startup: procure
   `event=app_startup_failed` nos logs e valide a configuração (ver
   `docs/operations/configuration.md`).
2. **`configuration` unavailable** — settings inválidos em runtime (improvável,
   pois falha cedo; verifique se o processo foi iniciado com configuração
   válida).
3. **`auth` unavailable** — o cliente real de autenticação não está disponível
   (a factory do engine falhou ou retornou `None`); autenticação em `/chat`
   e `/history` também falharia.
4. **`persistence` unavailable** — o cliente real de persistência não está
   disponível; admissão e histórico falhariam mesmo com o probe saudável.
5. **`database` unavailable** — verifique conectividade com o Supabase
   (URL, service role key, rede) e se a tabela `profiles` está acessível com
   o role da aplicação. Timeout curto sugere rede/slow query.
6. **`provider` unavailable** — chaves do provider ausentes ou inválidas na
   configuração da instância.
7. **`embeddings` unavailable** (só com `EMBEDDINGS_RETRIEVAL_ENABLED=true`) —
   o modelo não carregou no startup (ex.: `HF_HUB_OFFLINE=1` sem cache local)
   ou o processo foi iniciado sem o modelo necessário para o modo ativo.

Cada falha aparece nos logs como `event=readiness_check_failed
component=<nome>`; nenhum detalhe de exceção é registrado.
