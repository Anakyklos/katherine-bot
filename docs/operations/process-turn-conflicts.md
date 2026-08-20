# Runbook — conflitos do ProcessTurn (issue #272)

Operação do fluxo transacional de turno (`ProcessTurn`). Para o desenho
completo, veja `docs/architecture/process-turn-v1.md`.

## Precedência de conflitos

A precedência de decisão dentro de `commit_turn` (e refletida no ProcessTurn):

1. `account_deletion_pending` — conta do usuário em exclusão (tombstone no
   ledger, verificado por referência HMAC server-derived dentro do mesmo
   advisory lock da exclusão, logo após a aquisição do lock e antes de
   qualquer escrita). Sempre 423; nunca retry; **prevalece sobre todos os
   demais conflitos** por ser a barreira de primeiro passo do `commit_turn`.
2. `request_payload_conflict` — mesmo `(user_id, request_id)` com payload
   divergente (mensagem/estado/resposta diferentes). Sempre 409; nunca retry.
3. `request_in_progress` — linha `pending` com lease ativo de outro worker.
   409; nunca retry.
4. `lease_conflict` — corrida de claim/reclaim. 409; nunca retry.
5. `revision_mismatch` — CAS falhou. Retry interno limitado (1 repetição);
   esgotado → 409.
6. `request_replay_unavailable` — admissão repetida sem turno confirmado.
   409.
7. `PersistenceError` — falha inesperada de banco. 503; nunca expõe texto do
   PostgreSQL.
8. Deadline esgotado — 504; nunca dispara retry.
9. `CancelledError` — propagado; nunca vira 500.

Mapeamento HTTP centralizado em `backend/main.py::_map_process_turn_conflict`.
Mensagens públicas são constantes; request id, mensagem, revisões e texto do
banco nunca são devolvidos ao cliente.

## Sintomas e ações

| Sintoma | Causa provável | Ação |
|---|---|---|
| 423 `account_deletion_pending` | usuário solicitou exclusão de conta (tombstone no ledger); o endpoint `/chat` passou no preflight, mas o `commit_turn` atingiu a barreira no boundary transacional | sem retry; cliente trata como "conta indisponível"; o ledger registra a exclusão sob a mesma referência HMAC; raw user_id nunca participa do lookup (rejeitado por contrato hex-64) |
| 409 `revision_conflict` | dois turnos simultâneos do mesmo usuário | cliente reenvia com novo request id; sem ação manual |
| 409 `request_in_progress` | worker anterior morreu com lease ativo | aguardar expiração do lease; enquanto ativo, replay responde 409 |
| 409 `request_replay_unavailable` | request id repetido sem turno confirmado, ou reserva pendente com lease expirado | cliente usa novo request id; v1 não faz reclaim automático via endpoint |
| 503 `persistence_unavailable` | banco/PostgREST indisponível | verificar conectividade; retry com novo request id |
| 504 `turn_timeout` | orçamento de 16.000 unidades / deadline | reduzir mensagem; retry |
| evento `process_turn_conflict_exhausted` | 2 tentativas de CAS falharam | sinal de contenção alta; monitorar `event=process_turn_revision_conflict` |

## Reservas abandonadas (lease)

Quando um worker morre após criar a linha `pending`, o replay responde:

- `request_in_progress` enquanto o lease estiver ativo;
- `request_replay_unavailable` após a expiração do lease (a reserva nunca
  completa sozinha; o v1 **não** implementa reclaim automático via endpoint).

O dono da reserva (`lease_owner = process-turn-v1:<uuidhex>`) é per-instância
e nunca compartilhado entre workers; ele serve para diagnosticar qual
instância abandonou o turno. Para liberar o usuário, o cliente deve enviar um
novo request id.

## Logs úteis (baixa cardinalidade)

```
event=admission_admitted correlation=<hex64>
event=admission_replay correlation=<hex64>
event=process_turn_started correlation=<hex64>
event=process_turn_attempt attempt=1 correlation=<hex64>
event=process_turn_revision_conflict attempt=1 correlation=<hex64>
event=process_turn_replay correlation=<hex64>
event=process_turn_commit_completed attempt=N correlation=<hex64>
event=process_turn_conflict_exhausted attempt=N correlation=<hex64>
```

A `correlation` é um HMAC-SHA256 (64 hex minúsculos) do request id canônico,
calculado com o segredo de admissão sob domínio dedicado. Ela permite
correlacionar admissão → endpoint → ProcessTurn nos logs sem expor dados
sensíveis.

Nunca aparecem em logs: mensagem, resposta, prompt, contexto, memória,
user id, request id bruto, message id bruto, snapshots, exceções upstream,
tokens, payloads de RPC ou a própria `correlation` de outro turno.

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
