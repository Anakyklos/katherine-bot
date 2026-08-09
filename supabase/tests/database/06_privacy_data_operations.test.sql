BEGIN;

CREATE EXTENSION IF NOT EXISTS pgtap;
SELECT plan(116);

-- =================================================================
-- 1. Migration and ledger (#314)
-- =================================================================
SELECT ok(
    EXISTS(
        SELECT 1
        FROM supabase_migrations.schema_migrations
        WHERE name = 'privacy_data_operations'
    ),
    'privacy_data_operations migration is registered'
);

SELECT ok(
    EXISTS(
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = 'privacy_operations'
    ),
    'public.privacy_operations exists'
);

SELECT ok(
    (SELECT relrowsecurity FROM pg_class WHERE oid = 'public.privacy_operations'::regclass),
    'RLS is enabled on privacy_operations'
);
SELECT ok(
    (SELECT relforcerowsecurity FROM pg_class WHERE oid = 'public.privacy_operations'::regclass),
    'FORCE RLS is enabled on privacy_operations'
);
SELECT ok(
    NOT has_table_privilege('anon', 'public.privacy_operations',
        'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER'),
    'anon has no table privileges on privacy_operations'
);
SELECT ok(
    NOT has_table_privilege('authenticated', 'public.privacy_operations',
        'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER'),
    'authenticated has no table privileges on privacy_operations'
);
SELECT ok(
    NOT has_table_privilege('public', 'public.privacy_operations',
        'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER'),
    'PUBLIC has no table privileges on privacy_operations'
);
SELECT ok(
    NOT has_table_privilege('service_role', 'public.privacy_operations',
        'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER'),
    'service_role has no table privileges on privacy_operations (RPCs are the only path)'
);

-- =================================================================
-- 2. Function shape: exists, SECURITY DEFINER, fixed search_path
-- =================================================================
SELECT has_function('public', 'delete_history', ARRAY['text', 'uuid', 'jsonb'],
    'delete_history(text, uuid, jsonb) exists');
SELECT has_function('public', 'delete_memories', ARRAY['text', 'uuid', 'jsonb'],
    'delete_memories(text, uuid, jsonb) exists');
SELECT has_function('public', 'reset_emotional_state', ARRAY['text', 'uuid', 'jsonb'],
    'reset_emotional_state(text, uuid, jsonb) exists');
SELECT has_function('public', 'reset_relationship_state', ARRAY['text', 'uuid', 'jsonb'],
    'reset_relationship_state(text, uuid, jsonb) exists');
SELECT has_function('public', 'privacy_apply_operation', ARRAY['text', 'text', 'uuid', 'jsonb'],
    'privacy_apply_operation(text, text, uuid, jsonb) exists');
SELECT has_function('public', 'privacy_operation_payload_sha256', ARRAY['jsonb'],
    'privacy_operation_payload_sha256(jsonb) exists');
SELECT has_function('public', 'privacy_op_validation_error', ARRAY['text', 'uuid', 'jsonb'],
    'privacy_op_validation_error(text, uuid, jsonb) exists');

SELECT is(
    (SELECT prosecdef FROM pg_proc
     WHERE proname = 'delete_history' AND pronamespace = 'public'::regnamespace
       AND pronargs = 3),
    true,
    'delete_history is SECURITY DEFINER'
);
SELECT is(
    (SELECT prosecdef FROM pg_proc
     WHERE proname = 'delete_memories' AND pronamespace = 'public'::regnamespace
       AND pronargs = 3),
    true,
    'delete_memories is SECURITY DEFINER'
);
SELECT is(
    (SELECT prosecdef FROM pg_proc
     WHERE proname = 'reset_emotional_state' AND pronamespace = 'public'::regnamespace
       AND pronargs = 3),
    true,
    'reset_emotional_state is SECURITY DEFINER'
);
SELECT is(
    (SELECT prosecdef FROM pg_proc
     WHERE proname = 'reset_relationship_state' AND pronamespace = 'public'::regnamespace
       AND pronargs = 3),
    true,
    'reset_relationship_state is SECURITY DEFINER'
);

SELECT is(
    (SELECT COALESCE(
        (SELECT setting FROM pg_proc p
         CROSS JOIN LATERAL unnest(proconfig) AS cfg(setting)
         WHERE p.proname = 'delete_history' AND p.pronamespace = 'public'::regnamespace
           AND p.pronargs = 3 AND cfg.setting LIKE 'search_path=%'),
        '')
    ),
    'search_path=public',
    'delete_history has a fixed search_path = public'
);
SELECT is(
    (SELECT COALESCE(
        (SELECT setting FROM pg_proc p
         CROSS JOIN LATERAL unnest(proconfig) AS cfg(setting)
         WHERE p.proname = 'delete_memories' AND p.pronamespace = 'public'::regnamespace
           AND p.pronargs = 3 AND cfg.setting LIKE 'search_path=%'),
        '')
    ),
    'search_path=public',
    'delete_memories has a fixed search_path = public'
);
SELECT is(
    (SELECT COALESCE(
        (SELECT setting FROM pg_proc p
         CROSS JOIN LATERAL unnest(proconfig) AS cfg(setting)
         WHERE p.proname = 'reset_emotional_state' AND p.pronamespace = 'public'::regnamespace
           AND p.pronargs = 3 AND cfg.setting LIKE 'search_path=%'),
        '')
    ),
    'search_path=public',
    'reset_emotional_state has a fixed search_path = public'
);
SELECT is(
    (SELECT COALESCE(
        (SELECT setting FROM pg_proc p
         CROSS JOIN LATERAL unnest(proconfig) AS cfg(setting)
         WHERE p.proname = 'reset_relationship_state' AND p.pronamespace = 'public'::regnamespace
           AND p.pronargs = 3 AND cfg.setting LIKE 'search_path=%'),
        '')
    ),
    'search_path=public',
    'reset_relationship_state has a fixed search_path = public'
);

-- =================================================================
-- 3. ACLs: EXECUTE for service_role ONLY on the four public RPCs
-- =================================================================
SELECT function_privs_are(
    'public', 'delete_history', ARRAY['text', 'uuid', 'jsonb'],
    'anon', ARRAY[]::text[],
    'anon has no EXECUTE on delete_history'
);
SELECT function_privs_are(
    'public', 'delete_history', ARRAY['text', 'uuid', 'jsonb'],
    'authenticated', ARRAY[]::text[],
    'authenticated has no EXECUTE on delete_history'
);
SELECT function_privs_are(
    'public', 'delete_history', ARRAY['text', 'uuid', 'jsonb'],
    'service_role', ARRAY['EXECUTE'],
    'service_role has EXECUTE on delete_history'
);
SELECT ok(
    NOT has_function_privilege('public', 'public.delete_history(text, uuid, jsonb)', 'EXECUTE'),
    'PUBLIC has no EXECUTE on delete_history'
);

SELECT function_privs_are(
    'public', 'delete_memories', ARRAY['text', 'uuid', 'jsonb'],
    'anon', ARRAY[]::text[],
    'anon has no EXECUTE on delete_memories'
);
SELECT function_privs_are(
    'public', 'delete_memories', ARRAY['text', 'uuid', 'jsonb'],
    'authenticated', ARRAY[]::text[],
    'authenticated has no EXECUTE on delete_memories'
);
SELECT function_privs_are(
    'public', 'delete_memories', ARRAY['text', 'uuid', 'jsonb'],
    'service_role', ARRAY['EXECUTE'],
    'service_role has EXECUTE on delete_memories'
);
SELECT ok(
    NOT has_function_privilege('public', 'public.delete_memories(text, uuid, jsonb)', 'EXECUTE'),
    'PUBLIC has no EXECUTE on delete_memories'
);

SELECT function_privs_are(
    'public', 'reset_emotional_state', ARRAY['text', 'uuid', 'jsonb'],
    'anon', ARRAY[]::text[],
    'anon has no EXECUTE on reset_emotional_state'
);
SELECT function_privs_are(
    'public', 'reset_emotional_state', ARRAY['text', 'uuid', 'jsonb'],
    'authenticated', ARRAY[]::text[],
    'authenticated has no EXECUTE on reset_emotional_state'
);
SELECT function_privs_are(
    'public', 'reset_emotional_state', ARRAY['text', 'uuid', 'jsonb'],
    'service_role', ARRAY['EXECUTE'],
    'service_role has EXECUTE on reset_emotional_state'
);
SELECT ok(
    NOT has_function_privilege('public', 'public.reset_emotional_state(text, uuid, jsonb)', 'EXECUTE'),
    'PUBLIC has no EXECUTE on reset_emotional_state'
);

SELECT function_privs_are(
    'public', 'reset_relationship_state', ARRAY['text', 'uuid', 'jsonb'],
    'anon', ARRAY[]::text[],
    'anon has no EXECUTE on reset_relationship_state'
);
SELECT function_privs_are(
    'public', 'reset_relationship_state', ARRAY['text', 'uuid', 'jsonb'],
    'authenticated', ARRAY[]::text[],
    'authenticated has no EXECUTE on reset_relationship_state'
);
SELECT function_privs_are(
    'public', 'reset_relationship_state', ARRAY['text', 'uuid', 'jsonb'],
    'service_role', ARRAY['EXECUTE'],
    'service_role has EXECUTE on reset_relationship_state'
);
SELECT ok(
    NOT has_function_privilege('public', 'public.reset_relationship_state(text, uuid, jsonb)', 'EXECUTE'),
    'PUBLIC has no EXECUTE on reset_relationship_state'
);

-- Internal core/helpers: NO grants at all (owner postgres only).
SELECT function_privs_are(
    'public', 'privacy_apply_operation', ARRAY['text', 'text', 'uuid', 'jsonb'],
    'service_role', ARRAY[]::text[],
    'service_role has no EXECUTE on privacy_apply_operation'
);
SELECT ok(
    NOT has_function_privilege('public', 'public.privacy_apply_operation(text, text, uuid, jsonb)', 'EXECUTE'),
    'PUBLIC has no EXECUTE on privacy_apply_operation'
);
SELECT function_privs_are(
    'public', 'privacy_operation_payload_sha256', ARRAY['jsonb'],
    'service_role', ARRAY[]::text[],
    'service_role has no EXECUTE on privacy_operation_payload_sha256'
);
SELECT ok(
    NOT has_function_privilege('public', 'public.privacy_operation_payload_sha256(jsonb)', 'EXECUTE'),
    'PUBLIC has no EXECUTE on privacy_operation_payload_sha256'
);
SELECT function_privs_are(
    'public', 'privacy_op_validation_error', ARRAY['text', 'uuid', 'jsonb'],
    'service_role', ARRAY[]::text[],
    'service_role has no EXECUTE on privacy_op_validation_error'
);
SELECT ok(
    NOT has_function_privilege('public', 'public.privacy_op_validation_error(text, uuid, jsonb)', 'EXECUTE'),
    'PUBLIC has no EXECUTE on privacy_op_validation_error'
);

-- =================================================================
-- 3.1 Neutral snapshot verifier (reset semantics, issue #314 review)
-- =================================================================
SELECT has_function(
    'public', 'privacy_is_neutral_snapshot', ARRAY['jsonb', 'text'],
    'privacy_is_neutral_snapshot(jsonb, text) exists'
);
SELECT function_privs_are(
    'public', 'privacy_is_neutral_snapshot', ARRAY['jsonb', 'text'],
    'service_role', ARRAY[]::text[],
    'service_role has no EXECUTE on privacy_is_neutral_snapshot'
);
SELECT ok(
    NOT has_function_privilege('public', 'public.privacy_is_neutral_snapshot(jsonb, text)', 'EXECUTE'),
    'PUBLIC has no EXECUTE on privacy_is_neutral_snapshot'
);

-- Behavioral: the helper accepts the canonical neutral v1 output of the
-- Python domain constructors and rejects structurally valid non-neutral v1
-- snapshots. This pins the SQL constants to the domain so the two boundaries
-- cannot silently diverge.
SELECT ok(
    public.privacy_is_neutral_snapshot(
        '{"schema_version":1,"pleasure":0.0,"arousal":0.0,"dominance":0.0,"libido":0.0,"aggression":0.0,"connection":0.5,"energy":0.8,"tension":0.0,"coping_mode":"HEALTHY","timestamp":1700000000.0}'::jsonb,
        'emotional'
    ),
    'privacy_is_neutral_snapshot accepts the canonical neutral emotional v1 snapshot'
);
SELECT ok(
    NOT public.privacy_is_neutral_snapshot(
        '{"schema_version":1,"pleasure":0.9,"arousal":0.8,"dominance":0.7,"libido":0.1,"aggression":0.1,"connection":0.5,"energy":0.8,"tension":0.1,"coping_mode":"MANIC","timestamp":1700000000.0}'::jsonb,
        'emotional'
    ),
    'privacy_is_neutral_snapshot rejects a valid but non-neutral emotional v1 snapshot'
);
SELECT ok(
    public.privacy_is_neutral_snapshot(
        '{"schema_version":1,"trust":0.5,"affection":0.3,"tension":0.0,"triggers":[],"timestamp":1700000000.0}'::jsonb,
        'relationship'
    ),
    'privacy_is_neutral_snapshot accepts the canonical neutral relationship v1 snapshot'
);
SELECT ok(
    NOT public.privacy_is_neutral_snapshot(
        '{"schema_version":1,"trust":0.9,"affection":0.8,"tension":0.1,"triggers":[],"timestamp":1700000000.0}'::jsonb,
        'relationship'
    ),
    'privacy_is_neutral_snapshot rejects a valid but non-neutral relationship v1 snapshot'
);

-- =================================================================
-- 4. delete_history semantics (fixture user with full data)
-- =================================================================
INSERT INTO public.profiles (user_id, persona_config, user_profile,
    relationship_state, emotional_state, revision)
VALUES (
    'privacy_tap_user', 'persona-config', '{}'::jsonb,
    '{"schema_version":1,"trust":0.9,"affection":0.8,"tension":0.1,"triggers":[],"timestamp":1700000000.0}'::jsonb,
    '{"schema_version":1,"pleasure":0.9,"arousal":0.8,"dominance":0.7,"libido":0.1,"aggression":0.1,"connection":0.5,"energy":0.8,"tension":0.1,"coping_mode":"MANIC","timestamp":1700000000.0}'::jsonb,
    2
);

INSERT INTO public.chat_logs (user_id, role, content)
VALUES ('privacy_tap_user', 'user', 'hello'),
       ('privacy_tap_user', 'assistant', 'hi there');

INSERT INTO public.turn_requests (
    id, user_id, request_id, payload_hash_sha256, status, expected_revision,
    committed_revision, replay_payload, created_at, updated_at, completed_at
) VALUES (
    'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'::uuid,
    'privacy_tap_user', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'::uuid,
    'a'::text || repeat('0', 63), 'completed', 0, 2,
    '{"response":"hi there","message_id":"cccccccc-cccc-cccc-cccc-cccccccccccc"}'::jsonb,
    now(), now(), now()
);

INSERT INTO public.outbox_events (
    event_type, contract_version, user_id, turn_request_id, payload, status,
    attempts, next_attempt_at, idempotency_key, created_at, updated_at
) VALUES (
    'turn_completed', 1, 'privacy_tap_user',
    'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'::uuid,
    '{"ref":"t1"}'::jsonb, 'pending', 0, now() + interval '1 second',
    'privacy_tap_user_k1', now(), now()
);

INSERT INTO public.memories (user_id, content, metadata)
VALUES ('privacy_tap_user', 'a durable memory', '{"tags":["x"]}'::jsonb);

INSERT INTO public.archival_extractions (
    user_id, source_chat_log_id, extractor_version, schema_version,
    idempotency_key, facts
)
SELECT 'privacy_tap_user', id, 1, 1, 'privacy_tap_user_arch_1', '{"facts":[]}'::jsonb
FROM public.chat_logs
WHERE user_id = 'privacy_tap_user' AND role = 'user';

INSERT INTO public.admission_reservations (
    user_id, request_id, message_hmac_sha256, network_hmac_sha256, estimated_units
) VALUES (
    'privacy_tap_user', 'dddddddd-dddd-dddd-dddd-dddddddddddd'::uuid,
    repeat('a', 64), repeat('b', 64), 10
);

SELECT is(
    (SELECT public.delete_history(
        'privacy_tap_user', '11111111-1111-1111-1111-111111111111'::uuid, '{}'::jsonb
    )->>'status'),
    'applied',
    'delete_history returns status applied'
);
SELECT is(
    (SELECT public.delete_history(
        'privacy_tap_user', '11111111-1111-1111-1111-111111111111'::uuid, '{}'::jsonb
    )->>'operation'),
    'delete_history',
    'delete_history returns operation delete_history'
);
SELECT is(
    (SELECT public.delete_history(
        'privacy_tap_user', '11111111-1111-1111-1111-111111111111'::uuid, '{}'::jsonb
    )->>'revision')::bigint,
    3::bigint,
    'delete_history increments revision exactly once (2 -> 3)'
);
SELECT is(
    (SELECT (public.delete_history(
        'privacy_tap_user', '11111111-1111-1111-1111-111111111111'::uuid, '{}'::jsonb
    )->'counts'->>'chat_logs')::integer),
    2,
    'delete_history counts 2 chat_logs removed'
);
SELECT is(
    (SELECT (public.delete_history(
        'privacy_tap_user', '11111111-1111-1111-1111-111111111111'::uuid, '{}'::jsonb
    )->'counts'->>'turn_requests')::integer),
    1,
    'delete_history counts 1 turn_request removed'
);
SELECT is(
    (SELECT (public.delete_history(
        'privacy_tap_user', '11111111-1111-1111-1111-111111111111'::uuid, '{}'::jsonb
    )->'counts'->>'outbox_events')::integer),
    1,
    'delete_history counts 1 outbox_event removed'
);
SELECT is(
    (SELECT (public.delete_history(
        'privacy_tap_user', '11111111-1111-1111-1111-111111111111'::uuid, '{}'::jsonb
    )->'counts'->>'archival_extractions')::integer),
    1,
    'delete_history counts 1 archival extraction removed'
);
SELECT is(
    (SELECT (public.delete_history(
        'privacy_tap_user', '11111111-1111-1111-1111-111111111111'::uuid, '{}'::jsonb
    )->'counts'->>'memories')::integer),
    0,
    'delete_history never counts memories'
);

SELECT is(
    (SELECT count(*)::integer FROM public.chat_logs WHERE user_id = 'privacy_tap_user'),
    0,
    'chat_logs are removed by delete_history'
);
SELECT is(
    (SELECT count(*)::integer FROM public.turn_requests WHERE user_id = 'privacy_tap_user'),
    0,
    'turn_requests are removed by delete_history'
);
SELECT is(
    (SELECT count(*)::integer FROM public.outbox_events WHERE user_id = 'privacy_tap_user'),
    0,
    'outbox_events are removed by delete_history'
);
SELECT is(
    (SELECT count(*)::integer FROM public.archival_extractions WHERE user_id = 'privacy_tap_user'),
    0,
    'archival_extractions are removed by delete_history'
);
SELECT is(
    (SELECT count(*)::integer FROM public.memories WHERE user_id = 'privacy_tap_user'),
    1,
    'memories are PRESERVED by delete_history'
);
SELECT is(
    (SELECT count(*)::integer FROM public.admission_reservations WHERE user_id = 'privacy_tap_user'),
    1,
    'admission_reservations are PRESERVED by delete_history (no quota bypass)'
);
SELECT is(
    (SELECT count(*)::integer FROM public.profiles WHERE user_id = 'privacy_tap_user'),
    1,
    'profile is PRESERVED by delete_history'
);
SELECT is(
    (SELECT revision FROM public.profiles WHERE user_id = 'privacy_tap_user')::bigint,
    3::bigint,
    'profile revision is 3 after delete_history'
);
SELECT is(
    (SELECT emotional_state->>'coping_mode' FROM public.profiles WHERE user_id = 'privacy_tap_user'),
    'MANIC',
    'emotional snapshot is PRESERVED by delete_history'
);
SELECT is(
    (SELECT relationship_state->>'trust' FROM public.profiles WHERE user_id = 'privacy_tap_user'),
    '0.9',
    'relationship snapshot is PRESERVED by delete_history'
);

-- Replay: identical stored result, no mutation, no revision bump
SELECT is(
    (SELECT public.delete_history(
        'privacy_tap_user', '11111111-1111-1111-1111-111111111111'::uuid, '{}'::jsonb
    )),
    (SELECT public.delete_history(
        'privacy_tap_user', '11111111-1111-1111-1111-111111111111'::uuid, '{}'::jsonb
    )),
    'delete_history replay returns the identical stored result'
);
SELECT is(
    (SELECT revision FROM public.profiles WHERE user_id = 'privacy_tap_user')::bigint,
    3::bigint,
    'delete_history replay does not increment revision again'
);
SELECT is(
    (SELECT count(*)::integer FROM public.privacy_operations
     WHERE user_id = 'privacy_tap_user'
       AND operation_id = '11111111-1111-1111-1111-111111111111'::uuid),
    1,
    'exactly one durable ledger row after delete_history + replay'
);

-- Divergent payload / divergent operation on the same operation_id
SELECT is(
    (SELECT public.delete_history(
        'privacy_tap_user', '11111111-1111-1111-1111-111111111111'::uuid, '{"x":1}'::jsonb
    )->'error'->>'code'),
    'operation_conflict',
    'delete_history same operation_id with divergent payload conflicts'
);
SELECT is(
    (SELECT public.delete_memories(
        'privacy_tap_user', '11111111-1111-1111-1111-111111111111'::uuid, '{}'::jsonb
    )->'error'->>'code'),
    'operation_conflict',
    'same operation_id with a different operation conflicts'
);

-- =================================================================
-- 5. delete_memories semantics
-- =================================================================
INSERT INTO public.profiles (user_id, persona_config, user_profile,
    relationship_state, emotional_state, revision)
VALUES (
    'privacy_tap_mem', 'persona-config', '{}'::jsonb,
    '{"schema_version":1,"trust":0.5,"affection":0.3,"tension":0.0,"triggers":[],"timestamp":1700000000.0}'::jsonb,
    '{"schema_version":1,"pleasure":0.1,"arousal":0.2,"dominance":0.3,"libido":0.0,"aggression":0.0,"connection":0.5,"energy":0.8,"tension":0.0,"coping_mode":"HEALTHY","timestamp":1700000000.0}'::jsonb,
    4
);
INSERT INTO public.chat_logs (user_id, role, content)
VALUES ('privacy_tap_mem', 'user', 'hello'),
       ('privacy_tap_mem', 'assistant', 'hi there');
INSERT INTO public.memories (user_id, content, metadata)
VALUES ('privacy_tap_mem', 'a durable memory', '{}'::jsonb);
INSERT INTO public.archival_extractions (
    user_id, source_chat_log_id, extractor_version, schema_version,
    idempotency_key, facts
)
SELECT 'privacy_tap_mem', id, 1, 1, 'privacy_tap_mem_arch_1', '{"facts":[]}'::jsonb
FROM public.chat_logs
WHERE user_id = 'privacy_tap_mem' AND role = 'user';

SELECT is(
    (SELECT public.delete_memories(
        'privacy_tap_mem', '22222222-2222-2222-2222-222222222222'::uuid, '{}'::jsonb
    )->>'status'),
    'applied',
    'delete_memories returns status applied'
);
SELECT is(
    (SELECT (public.delete_memories(
        'privacy_tap_mem', '22222222-2222-2222-2222-222222222222'::uuid, '{}'::jsonb
    )->'counts'->>'memories')::integer),
    1,
    'delete_memories counts 1 memory removed'
);
SELECT is(
    (SELECT (public.delete_memories(
        'privacy_tap_mem', '22222222-2222-2222-2222-222222222222'::uuid, '{}'::jsonb
    )->'counts'->>'archival_extractions')::integer),
    1,
    'delete_memories counts 1 archival extraction removed'
);
SELECT is(
    (SELECT count(*)::integer FROM public.memories WHERE user_id = 'privacy_tap_mem'),
    0,
    'memories are removed by delete_memories'
);
SELECT is(
    (SELECT count(*)::integer FROM public.archival_extractions WHERE user_id = 'privacy_tap_mem'),
    0,
    'archival extractions are removed by delete_memories'
);
SELECT is(
    (SELECT count(*)::integer FROM public.chat_logs WHERE user_id = 'privacy_tap_mem'),
    2,
    'chat history is PRESERVED by delete_memories'
);
SELECT is(
    (SELECT revision FROM public.profiles WHERE user_id = 'privacy_tap_mem')::bigint,
    5::bigint,
    'delete_memories increments revision exactly once (4 -> 5)'
);
SELECT is(
    (SELECT public.delete_memories(
        'privacy_tap_mem', '22222222-2222-2222-2222-222222222222'::uuid, '{}'::jsonb
    )),
    (SELECT public.delete_memories(
        'privacy_tap_mem', '22222222-2222-2222-2222-222222222222'::uuid, '{}'::jsonb
    )),
    'delete_memories replay returns the identical stored result'
);
SELECT is(
    (SELECT revision FROM public.profiles WHERE user_id = 'privacy_tap_mem')::bigint,
    5::bigint,
    'delete_memories replay does not increment revision again'
);
SELECT is(
    (SELECT count(*)::integer FROM public.privacy_operations
     WHERE user_id = 'privacy_tap_mem'
       AND operation_id = '22222222-2222-2222-2222-222222222222'::uuid),
    1,
    'exactly one durable ledger row after delete_memories + replay'
);

-- =================================================================
-- 6. Reset semantics (v1 neutral snapshots, surgical replacement)
-- =================================================================
INSERT INTO public.profiles (user_id, persona_config, user_profile,
    relationship_state, emotional_state, revision)
VALUES (
    'privacy_tap_reset', 'persona-config', '{}'::jsonb,
    '{"schema_version":1,"trust":0.9,"affection":0.8,"tension":0.1,"triggers":[],"timestamp":1700000000.0}'::jsonb,
    '{"schema_version":1,"pleasure":0.9,"arousal":0.8,"dominance":0.7,"libido":0.1,"aggression":0.1,"connection":0.5,"energy":0.8,"tension":0.1,"coping_mode":"MANIC","timestamp":1700000000.0}'::jsonb,
    6
);
INSERT INTO public.chat_logs (user_id, role, content)
VALUES ('privacy_tap_reset', 'user', 'hello');
INSERT INTO public.memories (user_id, content, metadata)
VALUES ('privacy_tap_reset', 'a durable memory', '{}'::jsonb);

SELECT is(
    (SELECT public.reset_emotional_state(
        'privacy_tap_reset', '33333333-3333-3333-3333-333333333333'::uuid,
        '{"schema_version":1,"pleasure":0.0,"arousal":0.0,"dominance":0.0,"libido":0.0,"aggression":0.0,"connection":0.5,"energy":0.8,"tension":0.0,"coping_mode":"HEALTHY","timestamp":1700000000.0}'::jsonb
    )->>'status'),
    'applied',
    'reset_emotional_state returns status applied'
);
SELECT is(
    (SELECT revision FROM public.profiles WHERE user_id = 'privacy_tap_reset')::bigint,
    7::bigint,
    'reset_emotional_state increments revision exactly once (6 -> 7)'
);
SELECT is(
    (SELECT emotional_state FROM public.profiles WHERE user_id = 'privacy_tap_reset'),
    '{"schema_version":1,"pleasure":0.0,"arousal":0.0,"dominance":0.0,"libido":0.0,"aggression":0.0,"connection":0.5,"energy":0.8,"tension":0.0,"coping_mode":"HEALTHY","timestamp":1700000000.0}'::jsonb,
    'reset_emotional_state persists a valid v1 neutral snapshot'
);
SELECT is(
    (SELECT relationship_state->>'trust' FROM public.profiles WHERE user_id = 'privacy_tap_reset'),
    '0.9',
    'reset_emotional_state PRESERVES the relationship snapshot'
);
SELECT is(
    (SELECT count(*)::integer FROM public.chat_logs WHERE user_id = 'privacy_tap_reset'),
    1,
    'reset_emotional_state PRESERVES chat history'
);
SELECT is(
    (SELECT count(*)::integer FROM public.memories WHERE user_id = 'privacy_tap_reset'),
    1,
    'reset_emotional_state PRESERVES memories'
);
SELECT is(
    (SELECT public.reset_emotional_state(
        'privacy_tap_reset', '33333333-3333-3333-3333-333333333333'::uuid,
        '{"schema_version":1,"pleasure":0.0,"arousal":0.0,"dominance":0.0,"libido":0.0,"aggression":0.0,"connection":0.5,"energy":0.8,"tension":0.0,"coping_mode":"HEALTHY","timestamp":1700000000.0}'::jsonb
    )),
    (SELECT public.reset_emotional_state(
        'privacy_tap_reset', '33333333-3333-3333-3333-333333333333'::uuid,
        '{"schema_version":1,"pleasure":0.0,"arousal":0.0,"dominance":0.0,"libido":0.0,"aggression":0.0,"connection":0.5,"energy":0.8,"tension":0.0,"coping_mode":"HEALTHY","timestamp":1700000000.0}'::jsonb
    )),
    'reset_emotional_state replay returns the identical stored result'
);
SELECT is(
    (SELECT revision FROM public.profiles WHERE user_id = 'privacy_tap_reset')::bigint,
    7::bigint,
    'reset_emotional_state replay does not increment revision again'
);

SELECT is(
    (SELECT public.reset_relationship_state(
        'privacy_tap_reset', '44444444-4444-4444-4444-444444444444'::uuid,
        '{"schema_version":1,"trust":0.5,"affection":0.3,"tension":0.0,"triggers":[],"timestamp":1700000000.0}'::jsonb
    )->>'status'),
    'applied',
    'reset_relationship_state returns status applied'
);
SELECT is(
    (SELECT revision FROM public.profiles WHERE user_id = 'privacy_tap_reset')::bigint,
    8::bigint,
    'reset_relationship_state increments revision exactly once (7 -> 8)'
);
SELECT is(
    (SELECT relationship_state FROM public.profiles WHERE user_id = 'privacy_tap_reset'),
    '{"schema_version":1,"trust":0.5,"affection":0.3,"tension":0.0,"triggers":[],"timestamp":1700000000.0}'::jsonb,
    'reset_relationship_state persists a valid v1 neutral snapshot'
);
SELECT is(
    (SELECT emotional_state->>'coping_mode' FROM public.profiles WHERE user_id = 'privacy_tap_reset'),
    'HEALTHY',
    'reset_relationship_state PRESERVES the emotional snapshot'
);
SELECT is(
    (SELECT public.reset_relationship_state(
        'privacy_tap_reset', '44444444-4444-4444-4444-444444444444'::uuid,
        '{"schema_version":1,"trust":0.5,"affection":0.3,"tension":0.0,"triggers":[],"timestamp":1700000000.0}'::jsonb
    )),
    (SELECT public.reset_relationship_state(
        'privacy_tap_reset', '44444444-4444-4444-4444-444444444444'::uuid,
        '{"schema_version":1,"trust":0.5,"affection":0.3,"tension":0.0,"triggers":[],"timestamp":1700000000.0}'::jsonb
    )),
    'reset_relationship_state replay returns the identical stored result'
);
SELECT is(
    (SELECT revision FROM public.profiles WHERE user_id = 'privacy_tap_reset')::bigint,
    8::bigint,
    'reset_relationship_state replay does not increment revision again'
);

-- Invalid v1 snapshot is rejected without mutation or ledger record
SELECT is(
    (SELECT public.reset_emotional_state(
        'privacy_tap_reset', '55555555-5555-5555-5555-555555555555'::uuid,
        '{"schema_version":1,"pleasure":0.0,"arousal":0.0,"dominance":0.0,"libido":0.0,"aggression":0.0,"connection":0.5,"energy":0.8,"tension":0.0,"coping_mode":"BOGUS","timestamp":1700000000.0}'::jsonb
    )->'error'->>'code'),
    'validation_failed',
    'invalid v1 emotional snapshot is rejected with validation_failed'
);
SELECT is(
    (SELECT revision FROM public.profiles WHERE user_id = 'privacy_tap_reset')::bigint,
    8::bigint,
    'rejected reset does not change revision'
);
SELECT is(
    (SELECT count(*)::integer FROM public.privacy_operations
     WHERE user_id = 'privacy_tap_reset'
       AND operation_id = '55555555-5555-5555-5555-555555555555'::uuid),
    0,
    'rejected reset leaves no durable ledger row'
);

-- Valid v1 but semantically NON-NEUTRAL snapshots are rejected without any
-- mutation, revision bump or ledger record (issue #314 review).
SELECT is(
    (SELECT public.reset_emotional_state(
        'privacy_tap_reset', '77777777-7777-7777-7777-777777777777'::uuid,
        '{"schema_version":1,"pleasure":0.9,"arousal":0.8,"dominance":0.7,"libido":0.1,"aggression":0.1,"connection":0.5,"energy":0.8,"tension":0.1,"coping_mode":"MANIC","timestamp":1700000000.0}'::jsonb
    )->'error'->>'code'),
    'validation_failed',
    'valid but non-neutral emotional v1 snapshot is rejected with validation_failed'
);
SELECT is(
    (SELECT revision FROM public.profiles WHERE user_id = 'privacy_tap_reset')::bigint,
    8::bigint,
    'non-neutral emotional reset does not change revision'
);
SELECT is(
    (SELECT count(*)::integer FROM public.privacy_operations
     WHERE user_id = 'privacy_tap_reset'
       AND operation_id = '77777777-7777-7777-7777-777777777777'::uuid),
    0,
    'non-neutral emotional reset leaves no durable ledger row'
);
SELECT is(
    (SELECT public.reset_relationship_state(
        'privacy_tap_reset', '88888888-8888-8888-8888-888888888888'::uuid,
        '{"schema_version":1,"trust":0.9,"affection":0.8,"tension":0.1,"triggers":[],"timestamp":1700000000.0}'::jsonb
    )->'error'->>'code'),
    'validation_failed',
    'valid but non-neutral relationship v1 snapshot is rejected with validation_failed'
);
SELECT is(
    (SELECT revision FROM public.profiles WHERE user_id = 'privacy_tap_reset')::bigint,
    8::bigint,
    'non-neutral relationship reset does not change revision'
);
SELECT is(
    (SELECT count(*)::integer FROM public.privacy_operations
     WHERE user_id = 'privacy_tap_reset'
       AND operation_id = '88888888-8888-8888-8888-888888888888'::uuid),
    0,
    'non-neutral relationship reset leaves no durable ledger row'
);

-- =================================================================
-- 7. User isolation (A's operations never touch B, and vice versa)
-- =================================================================
INSERT INTO public.profiles (user_id, revision)
VALUES ('privacy_tap_other', 0);
INSERT INTO public.chat_logs (user_id, role, content)
VALUES ('privacy_tap_other', 'user', 'other hello');
INSERT INTO public.memories (user_id, content, metadata)
VALUES ('privacy_tap_other', 'other memory', '{}'::jsonb);

SELECT is(
    (SELECT public.delete_history(
        'privacy_tap_other', '66666666-6666-6666-6666-666666666666'::uuid, '{}'::jsonb
    )->>'status'),
    'applied',
    'delete_history for user B returns status applied'
);
SELECT is(
    (SELECT count(*)::integer FROM public.chat_logs WHERE user_id = 'privacy_tap_other'),
    0,
    'user B chat history is removed'
);
SELECT is(
    (SELECT count(*)::integer FROM public.chat_logs WHERE user_id = 'privacy_tap_reset'),
    1,
    'user B operations never touch user A chat history'
);
SELECT is(
    (SELECT count(*)::integer FROM public.memories WHERE user_id = 'privacy_tap_reset'),
    1,
    'user B operations never touch user A memories'
);

-- =================================================================
-- 8. SQL-side authenticated_user_id bounds (issue #314 review)
--    The SQL validation mirrors the persistent ledger contract
--    (char_length BETWEEN 1 AND 128 AND btrim <> ''): invalid identities
--    return a predictable validation envelope, never a generic P0001.
-- =================================================================
SELECT is(
    (SELECT public.delete_history(
        '   ', '99999999-9999-9999-9999-999999999998'::uuid, '{}'::jsonb
    )->'error'->>'code'),
    'validation_failed',
    'whitespace-only user_id fails with validation_failed (predictable)'
);
SELECT is(
    (SELECT public.delete_history(
        repeat('x', 129), '99999999-9999-9999-9999-999999999997'::uuid, '{}'::jsonb
    )->'error'->>'code'),
    'validation_failed',
    '129-character user_id fails with validation_failed (predictable)'
);
SELECT is(
    (SELECT public.delete_history(
        repeat('y', 128), '99999999-9999-9999-9999-999999999996'::uuid, '{}'::jsonb
    )->>'status'),
    'applied',
    '128-character user_id is accepted (boundary)'
);
SELECT is(
    (SELECT count(*)::integer FROM public.privacy_operations
     WHERE operation_id IN (
         '99999999-9999-9999-9999-999999999998'::uuid,
         '99999999-9999-9999-9999-999999999997'::uuid
     )),
    0,
    'invalid identities leave no durable ledger rows'
);

SELECT * FROM finish();
ROLLBACK;
