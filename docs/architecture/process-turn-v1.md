# ProcessTurn v1 — fluxo de turno idempotente e transacional

Status: implementado na issue #272 (PR de integração do ProcessTurn).
Depende de: #270 (schema transacional), #271 (commit atômico).

## Problema e causa raiz

O caminho ativo do `/chat` persistia um turno em várias operações separadas:

```text
load state        (MemoryManager.load_user_state — select → insert)
→ provider        (appraisal + geração via Groq)
→ save_turn       (insert de user/assistant em chat_logs)
→ sync_state      (update de perfis)
→ BackgroundTasks (extração arquivística)
```

Cada operação era uma transação independente: uma falha entre `save_turn` e
`sync_state` deixava mensagens órfãs e snapshots obsoletos; uma resposta HTTP
perdida não podia ser recuperada sem reprocessar o provider; e a contenção
entre workers não era comprovada (o `UserLockManager` só serializa dentro de
um único processo).

## Arquitetura

O novo fluxo substitui a dupla escrita por um único caso de uso:

```text
identidade autenticada
→ resultado de admissão
→ replay check quando aplicável
→ load state + revision          (leitura, sem insert)
→ load trusted context           (leitura autorizada)
→ appraisal/transitions/generation (FORA da transação)
→ commit_turn atômico com expected_revision (CAS)
→ retry limitado de revisão
→ retorna exatamente o resultado persistido
```

Módulos:

- `backend/process_turn.py` — caso de uso `ProcessTurn`, entrada imutável
  `ProcessTurnInput` (user id autenticado, request id canônico, mensagem
  validada, `TurnBudget`, `correlation`, modo `normal`/`replay_attempt`),
  resultado `ProcessTurnResult` construído **somente** a partir de
  `CommittedTurn.replay_payload`. Não possui estado por usuário em globais.
- `backend/turn_repositories.py` — fronteiras mínimas de repositório:
  - `UserStateRepository` — carrega snapshots + `revision` sem criar perfil;
  - `TurnCommitRepository` — chama `commit_turn` e interpreta `CommittedTurn`,
    `ConflictError`, `ValidationError`, `PersistenceError`;
  - `TurnReplayRepository` — recupera resultado já concluído sem provider.
- `backend/atomic_turn_commit.py` — contrato do RPC (inalterado; extraído o
  builder do payload canônico para reuso).
- `backend/chat_engine.py` — `ChatConversationEngine` delega o caminho ativo
  ao `ProcessTurn`; `ConversationEngine` (classe base) mantém o fluxo legado
  para testes/rollback, isolado do `/chat`.
- `supabase/migrations/20240101000006_process_turn_replay.sql` — RPC
  `replay_committed_turn`.

## Lease de reserva por instância

Cada instância do ProcessTurn recebe um `lease_owner` próprio
(`process-turn-v1:<uuidhex>` gerado por `new_lease_owner()`). O dono do lease:

- nunca é compartilhado entre instâncias/processos/workers (cada
  `ProcessTurn.execute()` usa um dono novo);
- identifica a reserva da linha `pending` criada por `commit_turn`, permitindo
  diagnosticar qual worker detém a reserva;
- não é usado para "dono único" global — a consistência continua vinda do CAS
  de revisão sob o lock de usuário no banco.

## Correlação sanitizada por turno

O `/chat` calcula `correlation` = HMAC-SHA256 do request id canônico sob o
segredo de admissão com domínio dedicado (`TURN_CORRELATION_DOMAIN`), em
`backend/admission.py` (`compute_turn_correlation`). O valor é exatamente 64
chars hex minúsculos e é o **único** valor derivado do request que o
ProcessTurn registra em logs/eventos.

A correlação:

- é estável entre a admissão, o endpoint e o ProcessTurn;
- nunca expõe o request id, user id, mensagem, segredo ou truncamento do UUID
  (é não reversível);
- aparece em todos os eventos do ProcessTurn
  (`process_turn_started`, `process_turn_commit_completed`,
  `process_turn_commit_conflict`, cancelamento, etc.) e no log de admissão
  (`event=admission_admitted` / `event=admission_replay`).

## Carregamento de estado com revisão

O caminho novo lê de `profiles`: `revision`, `persona_config`,
`user_profile`, `emotional_state`, `relationship_state`.

- Perfil existente → revisão persistida.
- Perfil ausente → estado padrão v1 em memória e `revision == 0`, **sem
  insert**; a criação inicial acontece somente dentro de `commit_turn`
  (race-safe, com lock de usuário no banco).
- Linha duplicada, revisão inválida (bool/negativa/float/string) ou snapshot
  malformado → falha fechada (`internal_error` sanitizado).
- Identidade vem exclusivamente do usuário autenticado; `user_id` dentro de
  JSON é sempre rejeitado pelo contrato do banco.

## Replay antes do provider

Quando a admissão retorna `request_replay_unavailable` (mesmo usuário repete
o mesmo `request_id` com a mesma mensagem), o `/chat` executa o ProcessTurn
em modo `replay_attempt`:

1. consulta `replay_committed_turn(user_id, request_id)`;
2. turno `completed` → retorna o `CommittedTurn` persistido;
3. sem carregar contexto, sem appraisal, sem Groq, sem transições, sem
   mensagens, sem outbox.

Resultados estruturados da RPC (nunca SQLSTATE/constraint/payload bruto):

| Estado                        | Resultado                        | HTTP |
|-------------------------------|----------------------------------|------|
| turno concluído               | envelope canônico `CommittedTurn`| 200  |
| linha pendente com lease ativo| `request_in_progress`            | 409  |
| reserva pendente com lease expirado | `request_replay_unavailable`| 409  |
| request expirado/falho        | `request_replay_unavailable`     | 409  |
| identidade divergente/inválida| erro sanitizado                  | 500/409 |

O builder canônico `commit_turn_build_result` é o único formato de replay —
commit novo e replay passam pelo mesmo parser
(`parse_commit_turn_result` / `parse_public_result`).

## CAS e retry limitado

O commit envia `expected_revision` = revisão carregada. O banco valida o CAS
sob lock de usuário (advisory 64-bit) e incrementa a revisão exatamente uma
vez por turno.

Política de retry — somente para `revision_mismatch`:

- no máximo uma repetição após a tentativa inicial (2 tentativas totais);
- verificar o orçamento antes de repetir (deadline esgotado → `DeadlineExceeded`);
- recarregar estado, revisão e contexto; recalcular appraisal, transições e
  geração — a resposta anterior nunca é reutilizada sobre estado obsoleto;
- registrar somente `event=process_turn_revision_conflict attempt=N`;
- nunca criar loop ilimitado; ao esgotar, `ConflictError("revision_mismatch")`
  recuperável e sanitizado, sem escrita parcial.

Nunca repete provider para: replay confirmado, `request_payload_conflict`,
`request_in_progress`, `lease_conflict`, entrada inválida, deadline esgotado,
cancelamento ou falha permanente do provider.

## Cancelamento e incerteza do commit

Antes do commit não existe escrita (mensagens/snapshots/outbox só nascem em
`commit_turn`), então cancelamento nesse estágio não grava nada.

Depois que `commit_turn` começa, a operação roda sob `run_blocking_write`
(protocolo de escrita não abandonável já existente): cancelamentos repetidos
são consumidos enquanto a operação é drenada; o resultado ou exceção é
recuperado obrigatoriamente; o lock local só é liberado após o término; e o
`CancelledError` original é propagado no final.

Se o commit foi confirmado mas a resposta HTTP se perdeu, o próximo envio com
o mesmo request id passa pela admissão (`request_replay_unavailable`) e
recupera o replay persistido sem provider.

## Papel do lock local

O `UserLockManager` continua existindo apenas como otimização local
(evita dois ProcessTurn concorrentes no mesmo processo para o mesmo usuário).
A consistência real vem do PostgreSQL (lock de usuário no banco + CAS de
revisão). Ele nunca é tratado como garantia multi-worker.

## Semântica multi-worker

- Duas requisições distintas do mesmo usuário carregando a mesma revisão não
  podem commitar ambas nessa revisão: uma vence, a outra recebe
  `revision_mismatch`, recarrega e commita na revisão seguinte.
- Usuários diferentes progridem independentemente (locks de usuário
  distintos — nunca bloqueio global).
- Criação concorrente do primeiro perfil produz uma única linha.
- Replay do mesmo request id após commit não gera nova transição, mensagem
  ou outbox e não chama provider.
- Provado por `test_process_turn_integration.py` com **processos
  independentes**, cada um instanciando o `ProcessTurn` real com repositórios
  e clientes Supabase separados (provider e context loader determinísticos,
  sem sleeps longos). A coordenação artificial é um rendezvous por arquivo no
  **commit** (primeira tentativa, `expected_revision == 0`), executado dentro
  de `run_blocking_write()` — que nunca abandona a escrita — então a espera
  não consome o orçamento pré-commit; ambos os workers carregam a revisão 0 e
  entram no primeiro commit antes de qualquer commit prosseguir. O worker que
  perde o CAS recebe `revision_mismatch`, recarrega estado/contexto e executa
  a segunda geração — o loop de retry vive dentro de `ProcessTurn.execute()`,
  nunca reimplementado no teste.

## Outbox em vez de BackgroundTasks

O caminho ativo não agenda `run_archival_extraction` em `BackgroundTasks`.
Quando `ARCHIVAL_EXTRACTION_ENABLED` está habilitado, o commit inclui
exatamente um evento idempotente de outbox:

```text
event_type: archival_extraction_requested
payload:    {"message_id": <request_id>, "kind": "archival", "version": 1}
idempotency_key: archival:<request_id>:v1
```

- contém somente referências sanitizadas: sem mensagem, user id, prompt,
  resposta, snapshots, HMAC ou segredo;
- nasce na mesma transação do turno (rollback remove);
- retry/replay nunca duplica o evento;
- o worker da outbox é fora de escopo desta issue.

## Comportamento quando a resposta HTTP é perdida

1. O commit foi confirmado (mensagens + snapshots + request + outbox).
2. O cliente reenvia o mesmo request id.
3. A admissão detecta repetição → `request_replay_unavailable`.
4. O `/chat` tenta replay → `completed` → resposta persistida, sem custo de
   provider e sem transições repetidas.

## Rollback operacional

- Reverter a PR restaura o fluxo legado (classe base `ConversationEngine`
  continua intacta); a migration 06 é aditiva e pode permanecer (a RPC só é
  chamada pelo caminho novo).
- Se necessário, `supabase db reset` em ambiente local; em produção, aplicar
  a migration 06 é seguro (nenhum objeto existente é alterado).
- `save_turn` / `sync_state` permanecem para testes legados, mas nunca são
  executados pelo `/chat`.

## Riscos residuais

- `revision_mismatch` após as 2 tentativas → 409 para o cliente; o usuário
  pode repetir a requisição (novo request id).
- Reserva `pending` de worker morto → 409 (`request_in_progress`) enquanto o
  lease estiver ativo; após a expiração do lease, o replay retorna
  `request_replay_unavailable`. O v1 **não** faz reclaim automático via
  endpoint: um request id abandonado exige um novo request id (ou ação
  operacional) para prosseguir. O dono do lease (`process-turn-v1:<uuidhex>`)
  permite diagnosticar qual instância abandonou a reserva.
- A extração arquivística fica pendente até o worker da outbox existir
  (fora de escopo).
- O lock local não protege entre processos; qualquer uso futuro deve
  continuar dependendo do CAS no banco.
