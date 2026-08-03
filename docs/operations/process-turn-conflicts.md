# Runbook — conflitos do ProcessTurn (issue #272)

Operação do fluxo transacional de turno (`ProcessTurn`). Para o desenho
completo, veja `docs/architecture/process-turn-v1.md`.

## Precedência de conflitos

A precedência de decisão dentro de `commit_turn` (e refletida no ProcessTurn):

1. `request_payload_conflict` — mesmo `(user_id, request_id)` com payload
   divergente (mensagem/estado/resposta diferentes). Sempre 409; nunca retry.
2. `request_in_progress` — linha `pending` com lease ativo de outro worker.
   409; nunca retry.
3. `lease_conflict` — corrida de claim/reclaim. 409; nunca retry.
4. `revision_mismatch` — CAS falhou. Retry interno limitado (1 repetição);
   esgotado → 409.
5. `request_replay_unavailable` — admissão repetida sem turno confirmado.
   409.
6. `PersistenceError` — falha inesperada de banco. 503; nunca expõe texto do
   PostgreSQL.
7. Deadline esgotado — 504; nunca dispara retry.
8. `CancelledError` — propagado; nunca vira 500.

Mapeamento HTTP centralizado em `backend/main.py::_map_process_turn_conflict`.
Mensagens públicas são constantes; request id, mensagem, revisões e texto do
banco nunca são devolvidos ao cliente.

## Sintomas e ações

| Sintoma | Causa provável | Ação |
|---|---|---|
| 409 `revision_conflict` | dois turnos simultâneos do mesmo usuário | cliente reenvia com novo request id; sem ação manual |
| 409 `request_in_progress` | worker anterior morreu com lease ativo | aguardar expiração do lease; o próximo commit reclaima |
| 409 `request_replay_unavailable` | request id repetido sem turno confirmado | cliente usa novo request id |
| 503 `persistence_unavailable` | banco/PostgREST indisponível | verificar conectividade; retry com novo request id |
| 504 `turn_timeout` | orçamento de 16.000 unidades / deadline | reduzir mensagem; retry |
| evento `process_turn_conflict_exhausted` | 2 tentativas de CAS falharam | sinal de contenção alta; monitorar `event=process_turn_revision_conflict` |

## Logs úteis (baixa cardinalidade)

```
event=process_turn_attempt attempt=1
event=process_turn_revision_conflict attempt=1
event=process_turn_replay
event=process_turn_commit_completed attempt=N
event=process_turn_conflict_exhausted attempt=N
```

Nunca aparecem em logs: mensagem, resposta, prompt, contexto, memória,
user id, request id bruto, message id bruto, snapshots, exceções upstream,
tokens ou payloads de RPC.

## Rollback operacional

- Reverter a PR restaura o fluxo legado (classe base `ConversationEngine`).
- A migration 06 é aditiva; manter ou reverter sem impacto para o fluxo
  legado.
- `save_turn`/`sync_state` continuam existindo para testes legados, mas não
  são executados pelo `/chat`.

## Verificação rápida pós-deploy

1. `supabase db reset` local + pgTAP (`supabase test db supabase/tests/database`).
2. `python -m pytest backend/tests/test_process_turn.py backend/tests/test_atomic_turn_commit.py`.
3. Com Supabase local no ar:
   `python -m pytest backend/tests/test_process_turn_integration.py`.
4. Confirmar no banco: `revision` incrementa 1 por turno; `outbox_events`
   nasce na mesma transação (rollback remove).
