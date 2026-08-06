# Configuration

Status: implementado na issue #275.

Este documento descreve os settings do backend: ambientes, variáveis
obrigatórias e opcionais, política de CORS, dados proibidos em logs e os
eventos de observabilidade permitidos.

## Modelo central

Toda configuração de runtime vive em `backend/settings.py` (pydantic v2
estrito, sem nova dependência). O modelo é congelado (`frozen=True`) e
validado também em atribuição:

- tipos e faixas explícitos (bool não aceita `"true"`/`1`; int não aceita
  `True`/`"5"`/`1.0`; números fora da faixa rejeitados);
- strings críticas não vazias; URLs validadas (http/https, sem credenciais,
  sem path/query);
- listas de origens CORS normalizadas (trim, sem `/` final, sem duplicatas);
- combinações inseguras rejeitadas (wildcard com credenciais, localhost em
  produção, feature habilitada sem dependência);
- campos secretos excluídos de `repr`, `str` e de erros de validação
  sanitizados (`SettingsConfigurationError` expõe apenas `campo:código`);
- construção direta inválida também não renderiza valores brutos
  (`hide_input_in_errors=True` no modelo);
- instância congelada nunca pode ser mutada para um estado inválido, e
  `ensure_valid()` reconstroi/revalida o modelo completo (usado pelo check
  `configuration` do readiness);
- configuração inválida falha antes de a aplicação servir tráfego.

Construção direta (`Settings(...)`) não lê o ambiente; `Settings.from_env()`
é o único ponto de leitura de variáveis de ambiente.

## Ambientes

`APP_ENV` é um enum fechado: `local`, `test`, `staging`, `production`.
Qualquer outro valor falha o startup, e **`APP_ENV` ausente ou vazio também
falha (fail-closed)**: um deploy que esqueça a variável nunca roda em modo
`local` por engano.

| Ambiente | Supabase obrigatório | CORS explícito obrigatório | localhost rejeitado |
| --- | --- | --- | --- |
| `local` | não (degrada com 503) | não (default `http://localhost:3000`) | não |
| `test` | não | não | não |
| `staging` | sim | sim | não |
| `production` | sim | sim | sim (origens e URL do Supabase) |

Docker/CI devem declarar `APP_ENV` explicitamente (o job `docker` da CI usa
`APP_ENV=test`). Produção usa `APP_ENV=production`.

## Variáveis de ambiente

### Obrigatórias (todos os ambientes)

| Variável | Descrição |
| --- | --- |
| `APP_ENV` | Ambiente (`local`, `test`, `staging`, `production`) — obrigatória |
| `GROQ_API_KEY` | Chave do provider (não vazia) |
| `ADMISSION_HMAC_SECRET` | Segredo do ledger de admissão (>= 32 bytes UTF-8) |

### Obrigatórias em `staging`/`production`

| Variável | Descrição |
| --- | --- |
| `SUPABASE_URL` | URL http(s) do Supabase |
| `SUPABASE_SERVICE_ROLE_KEY` | Service role key (não vazia) |
| `CORS_ALLOWED_ORIGINS` | Lista de origens separada por vírgulas (sem wildcard) |

### Opcionais

| Variável | Default | Descrição |
| --- | --- | --- |
| `GROQ_API_KEY_2` | ausente | Segunda chave do provider (pool) |
| `TRUSTED_PROXY_CIDRS` | vazio | CIDRs de proxies confiáveis (admissão) |
| `ARCHIVAL_EXTRACTION_ENABLED` | `false` | Habilita extração arquivística (`true`/`false` estritos) |
| `EMBEDDINGS_RETRIEVAL_ENABLED` | `false` | Habilita recuperação vetorial de memória (SentenceTransformer + RPC). Quando ligado, o modelo é construído no startup e o componente `embeddings` do `/ready` precisa passar; quando desligado, o modelo nunca é construído e a recuperação retorna vazio por design |
| `READINESS_DATABASE_TIMEOUT_MS` | `3000` | Timeout do check de banco (100–30000) |
| `READINESS_PROVIDER_TIMEOUT_MS` | `1000` | Timeout do check de provider (100–30000) |
| `TURN_TOTAL_DEADLINE` | `45.0` | Deadline do turno (validado por `TurnExecutionConfig`) |
| `TURN_CONNECT_TIMEOUT` | `3.0` | Timeout de conexão do provider |
| `TURN_PROVIDER_ATTEMPT_TIMEOUT` | `15.0` | Timeout de uma tentativa de geração |
| `TURN_SUPABASE_TIMEOUT` | `5.0` | Timeout por chamada PostgREST |
| `TURN_COMMIT_RESERVE` | `10.0` | Reserva do commit atômico |
| `TURN_MAX_ATTEMPTS` | `2` | Máx. tentativas do provider (1–5) |
| `TURN_BASE_BACKOFF` | `0.25` | Backoff base |
| `TURN_MAX_BACKOFF` | `0.75` | Backoff máximo |
| `TURN_MAX_JITTER` | `0.10` | Jitter (0–1) |
| `TURN_FRONTEND_TIMEOUT_MS` | `50000` | Sugestão de timeout do frontend (1000–300000) |

Não há chaves fictícias nem defaults silenciosos para segredos: ausência ou
valor vazio falha o startup.

## Política de CORS

- Origem permitida vem exclusivamente da allowlist validada dos settings.
- `*` com credenciais é rejeitado em todos os ambientes.
- Produção exige allowlist explícita e rejeita origens localhost
  (`localhost`, `127.0.0.1`, `::1`, `0.0.0.0`, sufixos `.localhost`/`.local`)
  e URL do Supabase em localhost.
- Nenhuma origem derivada de header do usuário é adicionada dinamicamente.
- Métodos permitidos: `GET`, `POST`, `OPTIONS` (o mínimo da aplicação).
- Headers permitidos: `Authorization`, `Content-Type` (o middleware do
  Starlette também anuncia os headers safelisted do CORS, comportamento
  padrão).
- Configurações `local` e `test` são explícitas nos settings (default
  documentado `http://localhost:3000`).

## Observabilidade

### Eventos

Nomes constantes em `backend/observability.py` (registry fechado):

```text
event=app_startup_failed
event=app_shutdown_failed
event=readiness_check_failed component=<nome>
event=auth_failed code=<código> duration_ms=<ms> correlation=<hmac>
event=auth_completed outcome=ok duration_ms=<ms> correlation=<hmac> user_ref=<hmac>
event=turn_completed code=ok duration_ms=<ms> mode=<normal|replay_attempt> correlation=<hmac>
event=turn_failed code=<código> correlation=<hmac>
event=request_conflict code=<código> correlation=<hmac>
event=http_result code=<status> correlation=<hmac>
```

Eventos de autenticação são medidos com relógio monotônico (`duration_ms`),
carregam a correlação sanitizada do request (derivada do request ID sob o
domínio dedicado) e, no sucesso, uma referência HMAC do usuário autenticado
(`user_ref`, domínio separado, não reversível). Ausência de credencial,
token inválido (4xx), 503 e erros inesperados emitem `auth_failed` de forma
consistente, sem token nem texto bruto do erro.

O fluxo transacional (#272) já emite eventos de fase no engine
(`event=turn_stage_completed stage=... outcome=...`, `process_turn_attempt`,
`process_turn_revision_conflict`, `process_turn_replay`,
`process_turn_commit_completed`) e de admissão (`admission_admitted`,
`admission_rejected`, `admission_replay`, `admission_unavailable`), todos com
correlação HMAC.

### Campos permitidos

`correlation`, `user_ref`, `code`, `stage`, `outcome`, `phase`,
`duration_ms`, `latency_ms`, `attempt`, `http_status`, `component`, `mode`,
`reason`, `result`, `retry`, `conflict`, `deadline_ms`.

### Dados proibidos em logs e erros

Nunca registre: mensagem, resposta, prompt, memória, appraisal bruto,
snapshot completo, token, segredo, exceção upstream bruta, URL com
credenciais, headers de autenticação, `user_id` bruto ou request ID bruto.

Correlação de request/turn usa o HMAC-SHA256 do request ID sob domínio
dedicado (`compute_turn_correlation`), e o usuário autenticado usa referência
HMAC sob domínio separado (`compute_user_reference`, `user_ref`), ambos com
segredo server-side; nunca SHA simples de identificador previsível e nunca o
`user_id`/request ID brutos.

`emit_event` rejeita (falha cedo) nomes de campo fora da allowlist ou
pertencentes à lista proibida. Nenhum módulo usa `logging.basicConfig()`.
