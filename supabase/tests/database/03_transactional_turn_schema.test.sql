BEGIN;

CREATE EXTENSION IF NOT EXISTS pgtap;
SELECT no_plan();

-- =================================================================
-- 0. Migration registration and empty-database application
-- =================================================================
-- The migration must be registered by supabase db reset (fresh apply) and
-- the pgTAP suite is the drift detector: any unexpected schema change makes
-- the exact-definition assertions below fail.
SELECT ok(
  EXISTS(
    SELECT 1 FROM supabase_migrations.schema_migrations
    WHERE version = '20240101000004'
  ),
  'transactional schema migration is registered'
);

-- =================================================================
-- 1. profiles.revision
-- =================================================================
SELECT has_column('public', 'profiles', 'revision', 'profiles has revision column');
SELECT col_not_null('public', 'profiles', 'revision', 'revision is NOT NULL');
SELECT col_type_is('public', 'profiles', 'revision', 'bigint', 'revision is bigint');
SELECT col_default_is('public', 'profiles', 'revision', '0', 'revision defaults to 0');
SELECT ok(
  EXISTS(
    SELECT 1 FROM pg_constraint
    WHERE conname = 'profiles_revision_non_negative_check'
      AND conrelid = 'profiles'::regclass
  ),
  'profiles has non-negative revision check'
);
SELECT ok(
  (SELECT pg_get_expr(conbin, conrelid)::text LIKE '%revision%>=%0%'
   FROM pg_constraint WHERE conname = 'profiles_revision_non_negative_check'),
  'revision check enforces non-negative values'
);

-- Behavior: new profile starts at revision 0
INSERT INTO public.profiles (user_id) VALUES ('pgtap_tr_user');
SELECT is(
  (SELECT revision FROM public.profiles WHERE user_id = 'pgtap_tr_user'),
  0::bigint,
  'new profile starts at revision 0'
);

-- Behavior: negative revision is rejected
SELECT throws_ok(
  $$INSERT INTO public.profiles (user_id, revision) VALUES ('pgtap_neg_rev', -1)$$,
  '23514', NULL, 'negative revision is rejected'
);

-- =================================================================
-- 2. turn_requests structure
-- =================================================================
SELECT has_table('public', 'turn_requests', 'turn_requests table exists');
SELECT has_column('public', 'turn_requests', 'id', 'turn_requests has id');
SELECT has_column('public', 'turn_requests', 'user_id', 'turn_requests has user_id');
SELECT has_column('public', 'turn_requests', 'request_id', 'turn_requests has request_id');
SELECT has_column('public', 'turn_requests', 'payload_hash_sha256', 'turn_requests has payload_hash_sha256');
SELECT has_column('public', 'turn_requests', 'status', 'turn_requests has status');
SELECT has_column('public', 'turn_requests', 'lease_owner', 'turn_requests has lease_owner');
SELECT has_column('public', 'turn_requests', 'lease_expires_at', 'turn_requests has lease_expires_at');
SELECT has_column('public', 'turn_requests', 'expected_revision', 'turn_requests has expected_revision');
SELECT has_column('public', 'turn_requests', 'committed_revision', 'turn_requests has committed_revision');
SELECT has_column('public', 'turn_requests', 'user_message_chat_log_id', 'turn_requests has user_message_chat_log_id');
SELECT has_column('public', 'turn_requests', 'assistant_message_chat_log_id', 'turn_requests has assistant_message_chat_log_id');
SELECT has_column('public', 'turn_requests', 'replay_payload', 'turn_requests has replay_payload');
SELECT has_column('public', 'turn_requests', 'error_code', 'turn_requests has error_code');
SELECT has_column('public', 'turn_requests', 'created_at', 'turn_requests has created_at');
SELECT has_column('public', 'turn_requests', 'updated_at', 'turn_requests has updated_at');
SELECT has_column('public', 'turn_requests', 'completed_at', 'turn_requests has completed_at');

SELECT col_not_null('public', 'turn_requests', 'user_id', 'user_id NOT NULL');
SELECT col_not_null('public', 'turn_requests', 'request_id', 'request_id NOT NULL');
SELECT col_not_null('public', 'turn_requests', 'payload_hash_sha256', 'payload_hash_sha256 NOT NULL');
SELECT col_not_null('public', 'turn_requests', 'status', 'status NOT NULL');
SELECT col_not_null('public', 'turn_requests', 'expected_revision', 'expected_revision NOT NULL');
SELECT col_type_is('public', 'turn_requests', 'request_id', 'uuid', 'request_id is uuid');
SELECT col_type_is('public', 'turn_requests', 'payload_hash_sha256', 'text', 'payload_hash_sha256 is text');
SELECT col_type_is('public', 'turn_requests', 'status', 'text', 'status is text');
SELECT col_type_is('public', 'turn_requests', 'expected_revision', 'bigint', 'expected_revision is bigint');
SELECT col_type_is('public', 'turn_requests', 'user_message_chat_log_id', 'bigint', 'user_message_chat_log_id is bigint');
SELECT col_type_is('public', 'turn_requests', 'replay_payload', 'jsonb', 'replay_payload is jsonb');

-- =================================================================
-- 3. turn_requests constraints
-- =================================================================
SELECT is(
  (SELECT pg_get_constraintdef(oid) FROM pg_constraint
   WHERE conname = 'turn_requests_user_id_request_id_key'),
  'UNIQUE (user_id, request_id)',
  'turn_requests unique constraint is (user_id, request_id)'
);
SELECT is(
  (SELECT pg_get_constraintdef(oid) FROM pg_constraint
   WHERE conname = 'turn_requests_user_id_id_key'),
  'UNIQUE (user_id, id)',
  'turn_requests has candidate key (user_id, id)'
);
SELECT ok(EXISTS(SELECT 1 FROM pg_constraint WHERE conname = 'turn_requests_status_check'), 'turn_requests has status check');
SELECT ok(EXISTS(SELECT 1 FROM pg_constraint WHERE conname = 'turn_requests_status_coherence_check'), 'turn_requests has status coherence check');
SELECT ok(EXISTS(SELECT 1 FROM pg_constraint WHERE conname = 'turn_requests_lease_pair_check'), 'turn_requests has lease pair check');
SELECT ok(EXISTS(SELECT 1 FROM pg_constraint WHERE conname = 'turn_requests_lease_owner_check'), 'turn_requests has lease owner check');
SELECT ok(EXISTS(SELECT 1 FROM pg_constraint WHERE conname = 'turn_requests_payload_hash_sha256_check'), 'turn_requests has payload hash check');
SELECT ok(EXISTS(SELECT 1 FROM pg_constraint WHERE conname = 'turn_requests_expected_revision_check'), 'turn_requests has expected revision check');
SELECT ok(EXISTS(SELECT 1 FROM pg_constraint WHERE conname = 'turn_requests_committed_revision_check'), 'turn_requests has committed revision check');
SELECT ok(EXISTS(SELECT 1 FROM pg_constraint WHERE conname = 'turn_requests_replay_payload_check'), 'turn_requests has replay payload check');
SELECT ok(EXISTS(SELECT 1 FROM pg_constraint WHERE conname = 'turn_requests_error_code_check'), 'turn_requests has error code check');

-- Composite message FKs enforce per-user isolation
SELECT is(
  (SELECT confdeltype FROM pg_constraint WHERE conname = 'turn_requests_user_message_chat_log_id_fkey'),
  'n'::"char",
  'user message FK has ON DELETE SET NULL'
);
SELECT is(
  (SELECT confdeltype FROM pg_constraint WHERE conname = 'turn_requests_assistant_message_chat_log_id_fkey'),
  'n'::"char",
  'assistant message FK has ON DELETE SET NULL'
);

-- =================================================================
-- 4. turn_requests behavior
-- =================================================================
-- Valid pending request with lease
INSERT INTO public.turn_requests (
  user_id, request_id, payload_hash_sha256, status,
  lease_owner, lease_expires_at, expected_revision
) VALUES (
  'pgtap_tr_user', '11111111-1111-4111-8111-111111111111', repeat('a', 64), 'pending',
  'worker-a', now() + interval '5 minutes', 0
);
SELECT is(
  (SELECT count(*)::integer FROM public.turn_requests WHERE user_id = 'pgtap_tr_user'),
  1,
  'valid pending request is inserted'
);

-- Duplicate (user_id, request_id) is rejected
SELECT throws_ok(
  $$INSERT INTO public.turn_requests (
       user_id, request_id, payload_hash_sha256, status,
       lease_owner, lease_expires_at
     ) VALUES (
       'pgtap_tr_user', '11111111-1111-4111-8111-111111111111', repeat('b', 64), 'pending',
       'worker-b', now() + interval '5 minutes'
     )$$,
  '23505', NULL, 'duplicate (user_id, request_id) is rejected'
);

-- Same request_id for a different user is allowed
INSERT INTO public.profiles (user_id) VALUES ('pgtap_tr_user_2');
INSERT INTO public.turn_requests (
  user_id, request_id, payload_hash_sha256, status,
  lease_owner, lease_expires_at
) VALUES (
  'pgtap_tr_user_2', '11111111-1111-4111-8111-111111111111', repeat('c', 64), 'pending',
  'worker-c', now() + interval '5 minutes'
);
SELECT is(
  (SELECT count(*)::integer FROM public.turn_requests
   WHERE request_id = '11111111-1111-4111-8111-111111111111'),
  2,
  'same request_id is allowed for different users'
);

-- Invalid status is rejected
SELECT throws_ok(
  $$INSERT INTO public.turn_requests (
       user_id, request_id, payload_hash_sha256, status,
       lease_owner, lease_expires_at
     ) VALUES (
       'pgtap_tr_user', gen_random_uuid(), repeat('d', 64), 'bogus',
       'worker-d', now() + interval '5 minutes'
     )$$,
  '23514', NULL, 'invalid request status is rejected'
);

-- Incoherent status/lease/completion combinations are rejected
SELECT throws_ok(
  $$INSERT INTO public.turn_requests (
       user_id, request_id, payload_hash_sha256, status
     ) VALUES (
       'pgtap_tr_user', gen_random_uuid(), repeat('e', 64), 'pending'
     )$$,
  '23514', NULL, 'pending without lease is rejected'
);
SELECT throws_ok(
  $$INSERT INTO public.turn_requests (
       user_id, request_id, payload_hash_sha256, status, completed_at
     ) VALUES (
       'pgtap_tr_user', gen_random_uuid(), repeat('f', 64), 'completed', now()
     )$$,
  '23514', NULL, 'completed without committed revision or replay payload is rejected'
);
SELECT throws_ok(
  $$INSERT INTO public.turn_requests (
       user_id, request_id, payload_hash_sha256, status
     ) VALUES (
       'pgtap_tr_user', gen_random_uuid(), repeat('1', 64), 'expired'
     )$$,
  '23514', NULL, 'expired without error code is rejected'
);
SELECT throws_ok(
  $$INSERT INTO public.turn_requests (
       user_id, request_id, payload_hash_sha256, status, lease_owner
     ) VALUES (
       'pgtap_tr_user', gen_random_uuid(), repeat('2', 64), 'pending', 'worker-h'
     )$$,
  '23514', NULL, 'lease owner without expiry is rejected'
);
SELECT throws_ok(
  $$INSERT INTO public.turn_requests (
       user_id, request_id, payload_hash_sha256, status, error_code
     ) VALUES (
       'pgtap_tr_user', gen_random_uuid(), repeat('7', 64), 'expired', 'Bad Error!'
     )$$,
  '23514', NULL, 'unsanitized error code is rejected'
);

-- FKs reject nonexistent references
SELECT throws_ok(
  $$INSERT INTO public.turn_requests (
       user_id, request_id, payload_hash_sha256, status,
       lease_owner, lease_expires_at, user_message_chat_log_id
     ) VALUES (
       'pgtap_tr_user', gen_random_uuid(), repeat('3', 64), 'pending',
       'worker-i', now() + interval '5 minutes', 999999
     )$$,
  '23503', NULL, 'FK rejects nonexistent chat_log reference'
);
SELECT throws_ok(
  $$INSERT INTO public.turn_requests (
       user_id, request_id, payload_hash_sha256, status,
       lease_owner, lease_expires_at
     ) VALUES (
       'pgtap_no_such_user', gen_random_uuid(), repeat('4', 64), 'pending',
       'worker-j', now() + interval '5 minutes'
     )$$,
  '23503', NULL, 'FK rejects nonexistent user reference'
);

-- Cross-user message reference is rejected (composite FK on (user_id, message_id))
INSERT INTO public.chat_logs (id, user_id, role, content)
VALUES (500002, 'pgtap_tr_user', 'user', 'cross-user isolation message');
SELECT throws_ok(
  $$INSERT INTO public.turn_requests (
       user_id, request_id, payload_hash_sha256, status,
       lease_owner, lease_expires_at, user_message_chat_log_id
     ) VALUES (
       'pgtap_tr_user_2', gen_random_uuid(), repeat('b', 64), 'pending',
       'worker-v', now() + interval '5 minutes', 500002
     )$$,
  '23503', NULL, 'cross-user message reference is rejected'
);

-- Valid completed request with a public replay contract is accepted
INSERT INTO public.turn_requests (
  user_id, request_id, payload_hash_sha256, status,
  completed_at, committed_revision, replay_payload
) VALUES (
  'pgtap_tr_user', '44444444-4444-4444-8444-444444444444', repeat('c', 64), 'completed',
  now(), 1, '{"response": "ok", "emotion_state": {"schema_version": 1}}'::jsonb
);
SELECT is(
  (SELECT count(*)::integer FROM public.turn_requests
   WHERE user_id = 'pgtap_tr_user' AND request_id = '44444444-4444-4444-8444-444444444444'),
  1,
  'valid completed request with public replay payload is inserted'
);

-- =================================================================
-- 5. turn_requests cascades (documented ON DELETE policy)
-- =================================================================
-- chat_log deletion nulls message references (SET NULL), keeps the request
INSERT INTO public.chat_logs (id, user_id, role, content)
VALUES (500001, 'pgtap_tr_user', 'user', 'cascade user message');
INSERT INTO public.turn_requests (
  user_id, request_id, payload_hash_sha256, status,
  lease_owner, lease_expires_at, user_message_chat_log_id
) VALUES (
  'pgtap_tr_user', '22222222-2222-4222-8222-222222222222', repeat('5', 64), 'pending',
  'worker-k', now() + interval '5 minutes', 500001
);
DELETE FROM public.chat_logs WHERE id = 500001;
SELECT is(
  (SELECT count(*)::integer FROM public.turn_requests
   WHERE user_id = 'pgtap_tr_user'
     AND request_id = '22222222-2222-4222-8222-222222222222'
     AND user_message_chat_log_id IS NULL),
  1,
  'chat_log deletion nulls message reference but keeps the request'
);

-- Profile deletion cascades turn_requests (no orphans)
DELETE FROM public.profiles WHERE user_id = 'pgtap_tr_user_2';
SELECT is(
  (SELECT count(*)::integer FROM public.turn_requests WHERE user_id = 'pgtap_tr_user_2'),
  0,
  'profile deletion cascades turn_requests (no orphans)'
);

-- =================================================================
-- 6. outbox_events structure
-- =================================================================
SELECT has_table('public', 'outbox_events', 'outbox_events table exists');
SELECT has_column('public', 'outbox_events', 'id', 'outbox_events has id');
SELECT has_column('public', 'outbox_events', 'event_type', 'outbox_events has event_type');
SELECT has_column('public', 'outbox_events', 'contract_version', 'outbox_events has contract_version');
SELECT has_column('public', 'outbox_events', 'user_id', 'outbox_events has user_id');
SELECT has_column('public', 'outbox_events', 'turn_request_id', 'outbox_events has turn_request_id');
SELECT has_column('public', 'outbox_events', 'payload', 'outbox_events has payload');
SELECT has_column('public', 'outbox_events', 'status', 'outbox_events has status');
SELECT has_column('public', 'outbox_events', 'attempts', 'outbox_events has attempts');
SELECT has_column('public', 'outbox_events', 'next_attempt_at', 'outbox_events has next_attempt_at');
SELECT has_column('public', 'outbox_events', 'lease_owner', 'outbox_events has lease_owner');
SELECT has_column('public', 'outbox_events', 'lease_expires_at', 'outbox_events has lease_expires_at');
SELECT has_column('public', 'outbox_events', 'idempotency_key', 'outbox_events has idempotency_key');
SELECT has_column('public', 'outbox_events', 'error_code', 'outbox_events has error_code');
SELECT has_column('public', 'outbox_events', 'processed_at', 'outbox_events has processed_at');
SELECT has_column('public', 'outbox_events', 'dead_lettered_at', 'outbox_events has dead_lettered_at');
SELECT has_column('public', 'outbox_events', 'retention_until', 'outbox_events has retention_until');

SELECT col_not_null('public', 'outbox_events', 'event_type', 'event_type NOT NULL');
SELECT col_not_null('public', 'outbox_events', 'user_id', 'user_id NOT NULL');
SELECT col_not_null('public', 'outbox_events', 'payload', 'payload NOT NULL');
SELECT col_not_null('public', 'outbox_events', 'status', 'status NOT NULL');
SELECT col_not_null('public', 'outbox_events', 'attempts', 'attempts NOT NULL');
SELECT col_not_null('public', 'outbox_events', 'idempotency_key', 'idempotency_key NOT NULL');
SELECT col_type_is('public', 'outbox_events', 'payload', 'jsonb', 'payload is jsonb');
SELECT col_type_is('public', 'outbox_events', 'contract_version', 'integer', 'contract_version is integer');
SELECT col_type_is('public', 'outbox_events', 'attempts', 'integer', 'attempts is integer');
SELECT col_default_is('public', 'outbox_events', 'contract_version', '1', 'contract_version defaults to 1');
SELECT col_default_is('public', 'outbox_events', 'attempts', '0', 'attempts defaults to 0');

-- =================================================================
-- 7. outbox_events constraints
-- =================================================================
SELECT is(
  (SELECT pg_get_constraintdef(oid) FROM pg_constraint
   WHERE conname = 'outbox_events_user_id_idempotency_key_key'),
  'UNIQUE (user_id, idempotency_key)',
  'outbox idempotency unique constraint is (user_id, idempotency_key)'
);
SELECT ok(EXISTS(SELECT 1 FROM pg_constraint WHERE conname = 'outbox_events_status_check'), 'outbox has status check');
SELECT ok(EXISTS(SELECT 1 FROM pg_constraint WHERE conname = 'outbox_events_status_coherence_check'), 'outbox has status coherence check');
SELECT ok(EXISTS(SELECT 1 FROM pg_constraint WHERE conname = 'outbox_events_attempts_check'), 'outbox has attempts check');
SELECT ok(EXISTS(SELECT 1 FROM pg_constraint WHERE conname = 'outbox_events_lease_pair_check'), 'outbox has lease pair check');
SELECT ok(EXISTS(SELECT 1 FROM pg_constraint WHERE conname = 'outbox_events_lease_owner_check'), 'outbox has lease owner check');
SELECT ok(EXISTS(SELECT 1 FROM pg_constraint WHERE conname = 'outbox_events_idempotency_key_check'), 'outbox has idempotency key check');
SELECT ok(EXISTS(SELECT 1 FROM pg_constraint WHERE conname = 'outbox_events_payload_check'), 'outbox has payload check');
SELECT ok(EXISTS(SELECT 1 FROM pg_constraint WHERE conname = 'outbox_events_error_code_check'), 'outbox has error code check');

-- Composite FK enforces per-user isolation for the request reference
SELECT is(
  (SELECT confdeltype FROM pg_constraint WHERE conname = 'outbox_events_turn_request_id_fkey'),
  'c'::"char",
  'outbox turn_request FK has ON DELETE CASCADE'
);

-- =================================================================
-- 8. outbox_events behavior
-- =================================================================
-- Valid pending event
INSERT INTO public.outbox_events (
  event_type, contract_version, user_id, turn_request_id, payload,
  status, attempts, next_attempt_at, idempotency_key
) VALUES (
  'memory_indexed', 1, 'pgtap_tr_user', NULL, '{"ref": "turn-1"}'::jsonb,
  'pending', 0, now(), 'pgtap-idem-1'
);
SELECT is(
  (SELECT count(*)::integer FROM public.outbox_events WHERE user_id = 'pgtap_tr_user'),
  1,
  'valid pending outbox event is inserted'
);

-- Duplicate idempotency key within the same user is rejected
SELECT throws_ok(
  $$INSERT INTO public.outbox_events (
       event_type, contract_version, user_id, turn_request_id, payload,
       status, attempts, next_attempt_at, idempotency_key
     ) VALUES (
       'memory_indexed', 1, 'pgtap_tr_user', NULL, '{"ref": "dup"}'::jsonb,
       'pending', 0, now(), 'pgtap-idem-1'
     )$$,
  '23505', NULL, 'duplicate outbox idempotency key is rejected'
);

-- Same idempotency key for a different user is allowed
INSERT INTO public.profiles (user_id) VALUES ('pgtap_tr_user_3');
INSERT INTO public.outbox_events (
  event_type, contract_version, user_id, turn_request_id, payload,
  status, attempts, next_attempt_at, idempotency_key
) VALUES (
  'memory_indexed', 1, 'pgtap_tr_user_3', NULL, '{"ref": "other-user"}'::jsonb,
  'pending', 0, now(), 'pgtap-idem-1'
);
SELECT is(
  (SELECT count(*)::integer FROM public.outbox_events WHERE idempotency_key = 'pgtap-idem-1'),
  2,
  'same idempotency key is allowed across users'
);

-- Invalid status is rejected
SELECT throws_ok(
  $$INSERT INTO public.outbox_events (
       event_type, contract_version, user_id, turn_request_id, payload,
       status, attempts, next_attempt_at, idempotency_key
     ) VALUES (
       'memory_indexed', 1, 'pgtap_tr_user', NULL, '{}'::jsonb,
       'bogus', 0, now(), 'pgtap-idem-bogus'
     )$$,
  '23514', NULL, 'invalid outbox status is rejected'
);

-- Invalid attempts are rejected
SELECT throws_ok(
  $$INSERT INTO public.outbox_events (
       event_type, contract_version, user_id, turn_request_id, payload,
       status, attempts, next_attempt_at, idempotency_key
     ) VALUES (
       'memory_indexed', 1, 'pgtap_tr_user', NULL, '{}'::jsonb,
       'failed', 11, now(), 'pgtap-idem-att11'
     )$$,
  '23514', NULL, 'attempts above 10 is rejected'
);
SELECT throws_ok(
  $$INSERT INTO public.outbox_events (
       event_type, contract_version, user_id, turn_request_id, payload,
       status, attempts, next_attempt_at, idempotency_key
     ) VALUES (
       'memory_indexed', 1, 'pgtap_tr_user', NULL, '{}'::jsonb,
       'pending', -1, now(), 'pgtap-idem-neg'
     )$$,
  '23514', NULL, 'negative attempts is rejected'
);

-- Invalid lease combinations are rejected
SELECT throws_ok(
  $$INSERT INTO public.outbox_events (
       event_type, contract_version, user_id, turn_request_id, payload,
       status, attempts, next_attempt_at, lease_owner, lease_expires_at, idempotency_key
     ) VALUES (
       'memory_indexed', 1, 'pgtap_tr_user', NULL, '{}'::jsonb,
       'pending', 0, now(), 'worker-x', now() + interval '5 minutes', 'pgtap-idem-lease'
     )$$,
  '23514', NULL, 'pending event with a lease is rejected'
);
SELECT throws_ok(
  $$INSERT INTO public.outbox_events (
       event_type, contract_version, user_id, turn_request_id, payload,
       status, attempts, next_attempt_at, idempotency_key
     ) VALUES (
       'memory_indexed', 1, 'pgtap_tr_user', NULL, '{}'::jsonb,
       'processing', 1, NULL, 'pgtap-idem-proc'
     )$$,
  '23514', NULL, 'processing event without a lease is rejected'
);
SELECT throws_ok(
  $$INSERT INTO public.outbox_events (
       event_type, contract_version, user_id, turn_request_id, payload,
       status, attempts, next_attempt_at, idempotency_key
     ) VALUES (
       'memory_indexed', 1, 'pgtap_tr_user', NULL, '{}'::jsonb,
       'completed', 1, NULL, 'pgtap-idem-compl'
     )$$,
  '23514', NULL, 'completed event without processed_at is rejected'
);
SELECT throws_ok(
  $$INSERT INTO public.outbox_events (
       event_type, contract_version, user_id, turn_request_id, payload,
       status, attempts, next_attempt_at, idempotency_key
     ) VALUES (
       'memory_indexed', 1, 'pgtap_tr_user', NULL, '{}'::jsonb,
       'dead_letter', 10, NULL, 'pgtap-idem-dl'
     )$$,
  '23514', NULL, 'dead_letter event without dead_lettered_at is rejected'
);

-- Exact state shapes: no field of another state can leak in
SELECT throws_ok(
  $$INSERT INTO public.outbox_events (
       event_type, contract_version, user_id, turn_request_id, payload,
       status, attempts, next_attempt_at, processed_at, retention_until,
       idempotency_key
     ) VALUES (
       'memory_indexed', 1, 'pgtap_tr_user', NULL, '{"ref":"x"}'::jsonb,
       'completed', 1, now(), now(), now() + interval '30 days',
       'pgtap-idem-cpl-na'
     )$$,
  '23514', NULL, 'completed event with next_attempt_at is rejected'
);
SELECT throws_ok(
  $$INSERT INTO public.outbox_events (
       event_type, contract_version, user_id, turn_request_id, payload,
       status, attempts, next_attempt_at, processed_at, dead_lettered_at,
       retention_until, idempotency_key
     ) VALUES (
       'memory_indexed', 1, 'pgtap_tr_user', NULL, '{"ref":"x"}'::jsonb,
       'completed', 1, NULL, now(), now(),
       now() + interval '30 days', 'pgtap-idem-cpl-dl'
     )$$,
  '23514', NULL, 'completed event with dead_lettered_at is rejected'
);
SELECT throws_ok(
  $$INSERT INTO public.outbox_events (
       event_type, contract_version, user_id, turn_request_id, payload,
       status, attempts, next_attempt_at, dead_lettered_at, retention_until,
       error_code, idempotency_key
     ) VALUES (
       'memory_indexed', 1, 'pgtap_tr_user', NULL, '{"ref":"x"}'::jsonb,
       'dead_letter', 10, NULL, now(), now() + interval '30 days',
       NULL, 'pgtap-idem-dl-noerr'
     )$$,
  '23514', NULL, 'dead_letter event without error_code is rejected'
);
SELECT throws_ok(
  $$INSERT INTO public.outbox_events (
       event_type, contract_version, user_id, turn_request_id, payload,
       status, attempts, next_attempt_at, dead_lettered_at, retention_until,
       error_code, idempotency_key
     ) VALUES (
       'memory_indexed', 1, 'pgtap_tr_user', NULL, '{"ref":"x"}'::jsonb,
       'dead_letter', 10, now(), now(), now() + interval '30 days',
       'exhausted', 'pgtap-idem-dl-na'
     )$$,
  '23514', NULL, 'dead_letter event with next_attempt_at is rejected'
);
SELECT throws_ok(
  $$INSERT INTO public.outbox_events (
       event_type, contract_version, user_id, turn_request_id, payload,
       status, attempts, next_attempt_at, dead_lettered_at,
       error_code, idempotency_key
     ) VALUES (
       'memory_indexed', 1, 'pgtap_tr_user', NULL, '{"ref":"x"}'::jsonb,
       'dead_letter', 10, NULL, now(),
       'exhausted', 'pgtap-idem-dl-noreten'
     )$$,
  '23514', NULL, 'dead_letter event without retention_until is rejected'
);
SELECT throws_ok(
  $$INSERT INTO public.outbox_events (
       event_type, contract_version, user_id, turn_request_id, payload,
       status, attempts, next_attempt_at, lease_owner, lease_expires_at,
       error_code, idempotency_key
     ) VALUES (
       'memory_indexed', 1, 'pgtap_tr_user', NULL, '{"ref":"x"}'::jsonb,
       'processing', 1, NULL, 'worker-y', now() + interval '5 minutes',
       'boom', 'pgtap-idem-proc-err'
     )$$,
  '23514', NULL, 'processing event with error_code is rejected'
);

-- Valid dead_letter event with a full exact shape is accepted
INSERT INTO public.outbox_events (
  event_type, contract_version, user_id, turn_request_id, payload,
  status, attempts, next_attempt_at, dead_lettered_at, retention_until,
  error_code, idempotency_key
) VALUES (
  'memory_indexed', 1, 'pgtap_tr_user', NULL, '{"ref":"turn-1"}'::jsonb,
  'dead_letter', 10, NULL, now(), now() + interval '30 days',
  'exhausted', 'pgtap-idem-dl-valid'
);
SELECT is(
  (SELECT count(*)::integer FROM public.outbox_events WHERE idempotency_key = 'pgtap-idem-dl-valid'),
  1,
  'valid dead_letter event is inserted'
);

-- Identifier bounds are enforced (fail-closed)
SELECT throws_ok(
  $$INSERT INTO public.turn_requests (
       user_id, request_id, payload_hash_sha256, status,
       lease_owner, lease_expires_at
     ) VALUES (
       'pgtap_tr_user', gen_random_uuid(), repeat('8', 64), 'pending',
       repeat('x', 65), now() + interval '5 minutes'
     )$$,
  '23514', NULL, 'turn_requests lease_owner over 64 chars is rejected'
);
SELECT throws_ok(
  $$INSERT INTO public.outbox_events (
       event_type, contract_version, user_id, turn_request_id, payload,
       status, attempts, next_attempt_at, idempotency_key
     ) VALUES (
       'memory_indexed', 1, 'pgtap_tr_user', NULL, '{}'::jsonb,
       'pending', 0, now(), repeat('k', 129)
     )$$,
  '23514', NULL, 'outbox idempotency_key over 128 chars is rejected'
);
SELECT throws_ok(
  $$INSERT INTO public.outbox_events (
       event_type, contract_version, user_id, turn_request_id, payload,
       status, attempts, next_attempt_at, idempotency_key
     ) VALUES (
       'memory_indexed', 1, 'pgtap_tr_user', NULL, '{}'::jsonb,
       'pending', 0, now(), 'bad key with space'
     )$$,
  '23514', NULL, 'outbox idempotency_key with invalid characters is rejected'
);

-- =================================================================
-- 9. Forbidden payload keys (prompt / internal fields) — top level and nested
-- =================================================================
SELECT throws_ok(
  $$INSERT INTO public.outbox_events (
       event_type, contract_version, user_id, turn_request_id, payload,
       status, attempts, next_attempt_at, idempotency_key
     ) VALUES (
       'memory_indexed', 1, 'pgtap_tr_user', NULL, '{"prompt": "hidden"}'::jsonb,
       'pending', 0, now(), 'pgtap-idem-prompt'
     )$$,
  '23514', NULL, 'outbox payload with prompt key is rejected'
);
SELECT throws_ok(
  $$INSERT INTO public.outbox_events (
       event_type, contract_version, user_id, turn_request_id, payload,
       status, attempts, next_attempt_at, idempotency_key
     ) VALUES (
       'memory_indexed', 1, 'pgtap_tr_user', NULL, '{"meta_cognition": {}}'::jsonb,
       'pending', 0, now(), 'pgtap-idem-meta'
     )$$,
  '23514', NULL, 'outbox payload with metacognition is rejected'
);
-- Nested forbidden keys are rejected at any depth
SELECT throws_ok(
  $$INSERT INTO public.outbox_events (
       event_type, contract_version, user_id, turn_request_id, payload,
       status, attempts, next_attempt_at, idempotency_key
     ) VALUES (
       'memory_indexed', 1, 'pgtap_tr_user', NULL,
       '{"ref": {"system_prompt": "hidden"}}'::jsonb,
       'pending', 0, now(), 'pgtap-idem-nested'
     )$$,
  '23514', NULL, 'nested system_prompt in outbox payload is rejected'
);
-- Message content can never be stored in the outbox
SELECT throws_ok(
  $$INSERT INTO public.outbox_events (
       event_type, contract_version, user_id, turn_request_id, payload,
       status, attempts, next_attempt_at, idempotency_key
     ) VALUES (
       'memory_indexed', 1, 'pgtap_tr_user', NULL,
       '{"ref": "turn-1", "message": "conteudo sensivel"}'::jsonb,
       'pending', 0, now(), 'pgtap-idem-msg'
     )$$,
  '23514', NULL, 'outbox payload with message content is rejected'
);
-- Unknown top-level keys are rejected by the allowlist
SELECT throws_ok(
  $$INSERT INTO public.outbox_events (
       event_type, contract_version, user_id, turn_request_id, payload,
       status, attempts, next_attempt_at, idempotency_key
     ) VALUES (
       'memory_indexed', 1, 'pgtap_tr_user', NULL,
       '{"ref": "turn-1", "unknown_key": 1}'::jsonb,
       'pending', 0, now(), 'pgtap-idem-unknown'
     )$$,
  '23514', NULL, 'outbox payload with unknown top-level key is rejected'
);

-- turn_requests replay_payload forbids internal fields too (top level + nested)
SELECT throws_ok(
  $$INSERT INTO public.turn_requests (
       user_id, request_id, payload_hash_sha256, status,
       completed_at, committed_revision, replay_payload, error_code
     ) VALUES (
       'pgtap_tr_user', gen_random_uuid(), repeat('6', 64), 'completed',
       now(), 1, '{"system_prompt": "hidden"}'::jsonb, NULL
     )$$,
  '23514', NULL, 'replay payload with system_prompt is rejected'
);
SELECT throws_ok(
  $$INSERT INTO public.turn_requests (
       user_id, request_id, payload_hash_sha256, status,
       completed_at, committed_revision, replay_payload, error_code
     ) VALUES (
       'pgtap_tr_user', gen_random_uuid(), repeat('0', 64), 'completed',
       now(), 1, '{"response": {"system_prompt": "hidden"}}'::jsonb, NULL
     )$$,
  '23514', NULL, 'nested system_prompt in replay payload is rejected'
);
SELECT throws_ok(
  $$INSERT INTO public.turn_requests (
       user_id, request_id, payload_hash_sha256, status,
       completed_at, committed_revision, replay_payload, error_code
     ) VALUES (
       'pgtap_tr_user', gen_random_uuid(), repeat('a', 64), 'completed',
       now(), 1, '{"response": "ok", "prompt": "hidden"}'::jsonb, NULL
     )$$,
  '23514', NULL, 'replay payload with prompt key is rejected'
);

-- FK: outbox turn_request_id rejects nonexistent reference
SELECT throws_ok(
  $$INSERT INTO public.outbox_events (
       event_type, contract_version, user_id, turn_request_id, payload,
       status, attempts, next_attempt_at, idempotency_key
     ) VALUES (
       'memory_indexed', 1, 'pgtap_tr_user', '99999999-9999-4999-8999-999999999999', '{}'::jsonb,
       'pending', 0, now(), 'pgtap-idem-fk'
     )$$,
  '23503', NULL, 'FK rejects nonexistent turn_request reference'
);

-- Cross-user turn_request reference is rejected (composite FK)
INSERT INTO public.turn_requests (
  user_id, request_id, payload_hash_sha256, status,
  lease_owner, lease_expires_at
) VALUES (
  'pgtap_tr_user', '55555555-5555-4555-8555-555555555555', repeat('d', 64), 'pending',
  'worker-y', now() + interval '5 minutes'
);
SELECT throws_ok(
  $$INSERT INTO public.outbox_events (
       event_type, contract_version, user_id, turn_request_id, payload,
       status, attempts, next_attempt_at, idempotency_key
     ) VALUES (
       'memory_indexed', 1, 'pgtap_tr_user_3',
       (SELECT id FROM public.turn_requests WHERE request_id = '55555555-5555-4555-8555-555555555555'),
       '{"ref":"turn-1"}'::jsonb,
       'pending', 0, now(), 'pgtap-idem-xuser'
     )$$,
  '23503', NULL, 'cross-user turn_request reference is rejected'
);

-- =================================================================
-- 10. RLS, grants and policies (server-owned internal tables)
-- =================================================================
SELECT ok((SELECT relrowsecurity FROM pg_class WHERE oid = 'public.turn_requests'::regclass), 'RLS enabled on turn_requests');
SELECT ok((SELECT relforcerowsecurity FROM pg_class WHERE oid = 'public.turn_requests'::regclass), 'FORCE RLS enabled on turn_requests');
SELECT ok((SELECT relrowsecurity FROM pg_class WHERE oid = 'public.outbox_events'::regclass), 'RLS enabled on outbox_events');
SELECT ok((SELECT relforcerowsecurity FROM pg_class WHERE oid = 'public.outbox_events'::regclass), 'FORCE RLS enabled on outbox_events');
SELECT policies_are('public', 'turn_requests', ARRAY[]::text[], 'turn_requests has no policies');
SELECT policies_are('public', 'outbox_events', ARRAY[]::text[], 'outbox_events has no policies');

-- anon / authenticated have no table privileges
SELECT table_privs_are('public', 'turn_requests', 'anon', ARRAY[]::text[]);
SELECT table_privs_are('public', 'turn_requests', 'authenticated', ARRAY[]::text[]);
SELECT table_privs_are('public', 'outbox_events', 'anon', ARRAY[]::text[]);
SELECT table_privs_are('public', 'outbox_events', 'authenticated', ARRAY[]::text[]);

-- service_role keeps full CRUD
SELECT table_privs_are('public', 'turn_requests', 'service_role', ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']);
SELECT table_privs_are('public', 'outbox_events', 'service_role', ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']);

-- PUBLIC has no privileges
SELECT ok(
  NOT has_table_privilege('public', 'public.turn_requests', 'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER'),
  'PUBLIC has no privileges on turn_requests'
);
SELECT ok(
  NOT has_table_privilege('public', 'public.outbox_events', 'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER'),
  'PUBLIC has no privileges on outbox_events'
);

-- Payload helpers are server-only (anon cannot execute)
SELECT ok(
  NOT has_function_privilege('anon', 'public.jsonb_has_forbidden_key(jsonb, text[])', 'EXECUTE'),
  'anon cannot execute jsonb_has_forbidden_key'
);
SELECT ok(
  NOT has_function_privilege('anon', 'public.jsonb_keys_subset_of(jsonb, text[])', 'EXECUTE'),
  'anon cannot execute jsonb_keys_subset_of'
);
SELECT ok(
  has_function_privilege('service_role', 'public.jsonb_has_forbidden_key(jsonb, text[])', 'EXECUTE'),
  'service_role can execute jsonb_has_forbidden_key'
);

-- =================================================================
-- 11. Indexes (exact definitions — equivalent verification for the
--     critical replay/claim queries)
-- =================================================================
SELECT has_index('public', 'turn_requests', 'turn_requests_user_id_request_id_key', 'turn_requests unique (user_id, request_id) index exists');
SELECT is(
  pg_get_indexdef('public.turn_requests_user_id_request_id_key'::regclass),
  'CREATE UNIQUE INDEX turn_requests_user_id_request_id_key ON public.turn_requests USING btree (user_id, request_id)',
  'unique (user_id, request_id) index has exact definition'
);
SELECT has_index('public', 'turn_requests', 'turn_requests_user_id_id_key', 'turn_requests unique (user_id, id) index exists');
SELECT is(
  pg_get_indexdef('public.turn_requests_user_id_id_key'::regclass),
  'CREATE UNIQUE INDEX turn_requests_user_id_id_key ON public.turn_requests USING btree (user_id, id)',
  'unique (user_id, id) index has exact definition'
);
SELECT has_index('public', 'turn_requests', 'turn_requests_user_id_created_at_idx', 'turn_requests user-created index exists');
SELECT is(
  pg_get_indexdef('public.turn_requests_user_id_created_at_idx'::regclass),
  'CREATE INDEX turn_requests_user_id_created_at_idx ON public.turn_requests USING btree (user_id, created_at DESC)',
  'user-created index has exact definition'
);
SELECT has_index('public', 'turn_requests', 'turn_requests_status_lease_expiry_idx', 'turn_requests status-lease index exists');
SELECT is(
  pg_get_indexdef('public.turn_requests_status_lease_expiry_idx'::regclass),
  'CREATE INDEX turn_requests_status_lease_expiry_idx ON public.turn_requests USING btree (status, lease_expires_at)',
  'status-lease index has exact definition'
);
SELECT has_index('public', 'turn_requests', 'turn_requests_user_committed_revision_idx', 'turn_requests revision index exists');
SELECT is(
  pg_get_indexdef('public.turn_requests_user_committed_revision_idx'::regclass),
  'CREATE INDEX turn_requests_user_committed_revision_idx ON public.turn_requests USING btree (user_id, committed_revision)',
  'revision index has exact definition'
);

SELECT has_index('public', 'outbox_events', 'outbox_events_user_id_idempotency_key_key', 'outbox unique (user_id, idempotency_key) index exists');
SELECT is(
  pg_get_indexdef('public.outbox_events_user_id_idempotency_key_key'::regclass),
  'CREATE UNIQUE INDEX outbox_events_user_id_idempotency_key_key ON public.outbox_events USING btree (user_id, idempotency_key)',
  'unique (user_id, idempotency_key) index has exact definition'
);
SELECT has_index('public', 'outbox_events', 'outbox_events_status_next_attempt_idx', 'outbox status-next_attempt index exists');
SELECT is(
  pg_get_indexdef('public.outbox_events_status_next_attempt_idx'::regclass),
  'CREATE INDEX outbox_events_status_next_attempt_idx ON public.outbox_events USING btree (status, next_attempt_at)',
  'status-next_attempt index has exact definition'
);
SELECT has_index('public', 'outbox_events', 'outbox_events_status_lease_expiry_idx', 'outbox status-lease index exists');
SELECT is(
  pg_get_indexdef('public.outbox_events_status_lease_expiry_idx'::regclass),
  'CREATE INDEX outbox_events_status_lease_expiry_idx ON public.outbox_events USING btree (status, lease_expires_at)',
  'status-lease index has exact definition'
);
SELECT has_index('public', 'outbox_events', 'outbox_events_turn_request_id_idx', 'outbox turn_request index exists');
SELECT is(
  pg_get_indexdef('public.outbox_events_turn_request_id_idx'::regclass),
  'CREATE INDEX outbox_events_turn_request_id_idx ON public.outbox_events USING btree (turn_request_id)',
  'turn_request index has exact definition'
);

-- =================================================================
-- 12. ON DELETE policy for every FK
-- =================================================================
SELECT is(
  (SELECT confdeltype FROM pg_constraint WHERE conname = 'turn_requests_user_id_fkey'),
  'c'::"char",
  'turn_requests.user_id FK has ON DELETE CASCADE'
);
SELECT is(
  (SELECT confdeltype FROM pg_constraint WHERE conname = 'outbox_events_user_id_fkey'),
  'c'::"char",
  'outbox_events.user_id FK has ON DELETE CASCADE'
);

-- =================================================================
-- 12b. Unexpected schema drift fails loudly
-- =================================================================
-- Re-creating any object that the migration owns must fail (the migration
-- preflight and PostgreSQL duplicate-object errors are the drift guard).
SELECT throws_ok(
  $$ALTER TABLE public.profiles ADD COLUMN revision bigint$$,
  '42701', NULL, 're-adding profiles.revision fails (drift)'
);
SELECT throws_ok(
  $$CREATE TABLE public.turn_requests (id uuid)$$,
  '42P07', NULL, 're-creating turn_requests fails (drift)'
);
SELECT throws_ok(
  $$CREATE TABLE public.outbox_events (id uuid)$$,
  '42P07', NULL, 're-creating outbox_events fails (drift)'
);
SELECT throws_ok(
  $$CREATE FUNCTION public.jsonb_has_forbidden_key(jsonb, text[]) RETURNS boolean
    LANGUAGE sql IMMUTABLE AS 'SELECT true'$$,
  '42723', NULL, 're-creating jsonb_has_forbidden_key fails (drift)'
);

-- =================================================================
-- 13. User deletion leaves no incompatible orphan data
-- =================================================================
INSERT INTO public.profiles (user_id) VALUES ('pgtap_orphan_user');
INSERT INTO public.turn_requests (
  user_id, request_id, payload_hash_sha256, status,
  lease_owner, lease_expires_at
) VALUES (
  'pgtap_orphan_user', '33333333-3333-4333-8333-333333333333', repeat('9', 64), 'pending',
  'worker-o', now() + interval '5 minutes'
);
INSERT INTO public.outbox_events (
  event_type, contract_version, user_id, turn_request_id, payload,
  status, attempts, next_attempt_at, idempotency_key
) VALUES (
  'memory_indexed', 1, 'pgtap_orphan_user', NULL, '{"ref": "orphan"}'::jsonb,
  'pending', 0, now(), 'pgtap-idem-orphan'
);
DELETE FROM public.profiles WHERE user_id = 'pgtap_orphan_user';
SELECT is(
  (SELECT count(*)::integer FROM public.turn_requests WHERE user_id = 'pgtap_orphan_user'),
  0,
  'profile deletion leaves no orphan turn_requests'
);
SELECT is(
  (SELECT count(*)::integer FROM public.outbox_events WHERE user_id = 'pgtap_orphan_user'),
  0,
  'profile deletion leaves no orphan outbox_events'
);

SELECT * FROM finish();
ROLLBACK;
