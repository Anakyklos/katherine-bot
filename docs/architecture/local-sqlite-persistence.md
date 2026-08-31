# Local SQLite Persistence (Katherine desktop) — #335

Estado: **implementado (fundação)**. Esta é a camada de persistência
local da Katherine desktop, extraída dos invariantes da arquitetura
PostgreSQL/Supabase sem transportar a complexidade multiusuário/cloud.
O modo web com Supabase permanece intocado; a fundação é o destino do
fluxo desktop (a integração do runtime desktop com esta camada é a
próxima folha, fora do escopo aqui).

## Localização

```text
~/.local/share/katherine/katherine.db
```

Caminho resolvido por ``default_database_path()`` seguindo
``XDG_DATA_HOME`` (com fallback ``~/.local/share``). O environment é
injetável (``env`` mapping) para testes; os diretórios são criados com
``0o700``. Override: qualquer caminho passado a
``open_local_storage(path)`` — os testes usam ``tmp_path`` com um banco
isolado por teste.

## Decisões explícitas

### Journaling e durability

| PRAGMA | Valor | Motivo |
|---|---|---|
| `journal_mode` | `WAL` | Leitores nunca bloqueiam escritor (snapshot consistente por leitura), recuperação pós-crash automática pelo próprio SQLite, backup online consistente via `Connection.backup()`. |
| `synchronous` | `FULL` | Sem réplica/replay: o arquivo local é a única cópia. `FULL` garante que um `COMMIT` retornado sobreviva a queda de energia no dispositivo. O custo é aceitável no padrão de escrita da Katherine (1 commit por turno). |
| `foreign_keys` | `ON` | Integridade referencial real em toda conexão, não advisory. |
| `busy_timeout` | 5s | Contenção transitória leitor/escritor dentro do processo. |

### Concorrência (política explícita)

**Serialização por lock de processo + `BEGIN IMMEDIATE` prévio à
primeira escrita.**

- A store possui um ``threading.RLock``; todo caminho de escrita o toma.
- A transação abre com ``BEGIN IMMEDIATE`` — o lock de escrita é
  adquirido **antes** de qualquer statement, eliminando a janela em que
  um upgrade deferred pode falhar com ``SQLITE_BUSY`` no meio de um
  turno (atomicidade ameaçada).
- Cada thread tem sua própria conexão (``threading.local``); conexões
  SQLite não são compartilháveis entre threads. Com WAL, leitores sempre
  veem o último **snapshot commitado** — nunca um turno em andamento.
- **Outro processo não faz parte do contrato**: o banco pertence ao
  processo da aplicação desktop (single-user, sem daemon). Isso é a
  substituição local dos advisory locks distribuídos: o invariante
  "coordenação" é garantido pela posse única da conexão escrevente, e
  não por lock distribuído.

### Recovery

Na abertura, todo ``turn_requests`` com ``status='pending'`` é marcado
``failed`` com ``error_code='interrupted'`` numa única transação. Um
turno pendente sem escritor vivo só pode existir após crash; a política
é **fail-closed** (paridade com o contrato web
``request_replay_unavailable``: nunca reexecuta automaticamente). O WAL
presente no disco é recuperado pelo próprio SQLite.

### Corrupção e I/O

- Corrupção (``sqlite3.DatabaseError`` na abertura) →
  ``StorageCorruptError``. O arquivo **não é nunca recriado
  silenciosamente** — reset destruiria dados do usuário sem consentimento.
- Erros de I/O/permissão → ``PersistenceError`` com mensagem constante
  sanitizada. Nenhum SQL, path, conteúdo de mensagem, traceback ou
  detalhe do driver atravessa a fronteira pública.

## Commit atômico do turno

Uma única transação ``BEGIN IMMEDIATE`` … ``COMMIT`` executa, nesta ordem:

1. **Idempotência**: request_id já ``completed`` → retorna o turn
   commitado original sem escrever nada (replay idempotente);
2. **CAS**: lê ``profiles.revision``; se ``expected_revision`` difere →
   ``ConflictError("revision_mismatch")`` com rollback integral;
3. INSERT mensagem do usuário (``chat_logs``);
4. INSERT mensagem da assistente (``chat_logs``);
5. UPSERT do perfil com ``revision = expected + 1`` e os snapshots
   emocional/relacional do turno;
6. INSERT do ledger ``turn_requests`` (``status='completed'``,
   ``replay_payload`` canônico);
7. INSERT dos ``outbox_events`` (``ON CONFLICT (idempotency_key) DO
   NOTHING``).

Qualquer falha entre 2 e 7 → rollback do turno inteiro: mensagens e
snapshots jamais divergem (testes 3 e 4 da issue). O CAS substitui o
lock transacional per-user do PostgreSQL: o invariante "uma escrita
obsoleta nunca sobrescreve estado novo" é preservado pelo mesmo mecanismo
lógico (version token), agora guardado por transação serializada.

## Migrations

- Em código: lista frozen ``(version, sql)`` em
  ``backend/local_storage/migrations.py`` — ordenadas, append-only,
  reprodutíveis, sem dependência nova e sem arquivos soltos.
- ``schema_migrations(version PK, applied_at)`` registra as aplicadas;
  reabrir pula as existentes (idempotente).
- **Atomicidade por migration**: cada migration roda em sua própria
  transação junto com a inserção da sua linha de versão — uma migration
  que falha no meio não fica nem aplicada nem marcada (teste 8). A
  próxima abertura a retria do início.

## Mapeamento de dados (PostgreSQL → SQLite)

| Dado (origem) | Destino local | Notas |
|---|---|---|
| `profiles` (persona, user_profile, emocional, relacional, revision) | `profiles` (1 linha, `id=1`) | Snapshots como JSON canônico; `revision` mantém o CAS |
| `chat_logs` | `chat_logs` | Sem `user_id` (single-user); índice `(created_at, id)` para recência |
| `turn_requests` | `turn_requests` | Ledger de idempotência/replay; FKs para `chat_logs` `ON DELETE SET NULL` (privacidade nunca é bloqueada pelo ledger, paridade com o trigger do PostgreSQL) |
| `outbox_events` | `outbox_events` | Idempotency key única; payload estrito de referências (sem conteúdo) |
| `memories` | `memories` | Metadados JSON com o mesmo contrato de versão/aprovação |
| `privacy_operations` | `privacy_operations` | Auditoria local (operação, status, result) sem conteúdo privado |
| `account_deletion_jobs` / worker | **removido nesta fundação** | Destruição de conta é o equivalente local a apagar o arquivo do banco; a operação é documentada, o worker distribuído não se aplica |
| `admission_reservations` / HMAC de identidade | **removido nesta fundação** | Admissão/rate-limit e correlação HMAC eram proteções do serviço multiusuário remoto; o runtime desktop decide o uso deles na próxima folha |

## Invariantes: preservados x removidos

| Invariante | Status | Como |
|---|---|---|
| Commit do turno como unidade atômica | **Preservado** | `BEGIN IMMEDIATE` + rollback integral |
| Mensagens e snapshots não divergem após crash | **Preservado** | Transação única + WAL + `synchronous=FULL` |
| Schema versionado, migrations reproduzíveis | **Preservado** | Lista em código + `schema_migrations` |
| Integridade referencial | **Preservado** | `PRAGMA foreign_keys=ON` + FKs com ON DELETE coerentes |
| Revision para detectar estado obsoleto | **Preservado** | CAS em `profiles.revision` |
| Idempotência/replay | **Preservado** | `turn_requests` PK + replay do commitado |
| Recuperação previsível pós-interrupção | **Preservado** | WAL auto-recovery + pending→failed fail-closed |
| Retenção e exclusão reais | **Preservado** | `delete_history`/`delete_memories`/`trim_history` + auditoria |
| Limites de tamanho/crescimento | **Preservado** | `MAX_MESSAGE_LENGTH`, trim por contagem, métricas |
| Erros sanitizados | **Preservado** | code+message constantes; sem SQL/path/conteúdo |
| Nada importante só em RAM | **Preservado** | Toda escrita de turno é transação commitada |
| RLS multiusuário | **Removido** | Single-user local; o arquivo é a fronteira de confiança |
| service-role ACLs / grants | **Removido** | Idem; sem papéis no SQLite |
| Advisory locks distribuídos / réplicas | **Removido** | Um processo, um escrevente, lock de processo |
| HMAC de identidade (`user_id` remoto) | **Removido** | Sem identidade multiusuário para esconder |
| Lease/claim entre workers | **Removido** | Substituído pelo lock de processo + pending→failed |
| PostgREST/RPC | **Removido** | Chamadas diretas à store local |

## Importação futura (Supabase → SQLite) — estratégia definida

A importação de uma instalação existente **não é implementada nesta
entrega**; o formato e a política são definidos aqui e a execução é
folha futura:

1. **Explícita**: comando dedicado (CLI), nunca automática no primeiro
   boot.
2. **Validação de origem**: verifica schema version da origem e o
   conjunto esperado de tabelas antes de ler; origem incompatível
   aborta sem escrever.
3. **Idempotente**: chave de importação por tabela (contagens +
   hash canônico por row na tabela de auditoria local); reexecutar
   detecta "já importado" e é no-op.
4. **Origem preservada**: a origem nunca é apagada nem modificada antes
   de verificação pós-importação integral (contagens por tabela
   conferidas).
5. **Evidência sem conteúdo privado**: o relatório importa apenas
   contagens por tabela, duração e versões; nenhum conteúdo de
   mensagem/memória entra em log.

## Testes

``backend/tests/test_local_storage.py`` — 35 testes, todos contra
arquivos SQLite **reais temporários** (um banco isolado por teste,
``tmp_path``). Falhas são produzidas por mecânica SQLite real (triggers
de abort, CHECK, CAS), nunca por mocks. Mapeamento direto dos 12
testes obrigatórios da issue + extras (CAS, replay, outbox, backup,
XDG, sanitized, no-cloud-imports, recovery de pending).

Nenhum teste exige Supabase/Postgres/rede; o teste de import
verifica por subprocess que o pacote não puxa `supabase`/`postgrest`/
`httpx`/`requests` ao ser importado.

## Fora do escopo desta fundação

- Integração do runtime desktop com esta store (próxima folha);
- Remoção do Supabase/Auth do backend (#336);
- Importação de produção;
- Vector search local (a coluna/metadata existe; o retrieval local é
  redesign de memória, folha futura);
- Packaging Linux.
