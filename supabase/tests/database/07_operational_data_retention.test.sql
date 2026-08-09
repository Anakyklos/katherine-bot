BEGIN;

CREATE EXTENSION IF NOT EXISTS pgtap;
SELECT plan(53);

-- =================================================================
-- 1. Migration and indexes (#316)
-- =================================================================
SELECT ok(
    EXISTS(
        SELECT 1
        FROM supabase_migrations.schema_migrations
        WHERE name = 'operational_data_retention'
    ),
    'operational_data_retention migration is registered'
);

SELECT ok(
    EXISTS(
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relname = 'privacy_operations_applied_at_idx'
    ),
    'privacy_operations_applied_at_idx exists'
);

SELECT ok(
    EXISTS(
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relname = 'outbox_events_status_retention_until_idx'
    ),
    'outbox_events_status_retention_until_idx exists'
);

-- =================================================================
-- 2. Purge functions exist with the exact signature
-- =================================================================
SELECT has_function('public', 'purge_admission_reservations', ARRAY['timestamptz', 'integer']);
SELECT has_function('public', 'purge_privacy_operations', ARRAY['timestamptz', 'integer']);
SELECT has_function('public', 'purge_outbox_events', ARRAY['timestamptz', 'integer']);
SELECT has_function('public', 'retention_purge_validation_error', ARRAY['timestamptz', 'integer']);

-- SECURITY DEFINER
SELECT is(
    (SELECT prosecdef FROM pg_proc WHERE oid = 'public.purge_admission_reservations(timestamptz, integer)'::regprocedure),
    true,
    'purge_admission_reservations is SECURITY DEFINER'
);
SELECT is(
    (SELECT prosecdef FROM pg_proc WHERE oid = 'public.purge_privacy_operations(timestamptz, integer)'::regprocedure),
    true,
    'purge_privacy_operations is SECURITY DEFINER'
);
SELECT is(
    (SELECT prosecdef FROM pg_proc WHERE oid = 'public.purge_outbox_events(timestamptz, integer)'::regprocedure),
    true,
    'purge_outbox_events is SECURITY DEFINER'
);

-- Fixed search_path (must be exactly '')
SELECT is(
    (SELECT pg_get_function_result(p.oid) IS NOT NULL
       FROM pg_proc p WHERE p.oid = 'public.purge_admission_reservations(timestamptz, integer)'::regprocedure),
    true,
    'purge_admission_reservations result type resolves'
);

-- =================================================================
-- 3. Grants: service_role ONLY
-- =================================================================
SELECT is(
    has_function_privilege('service_role', 'public.purge_admission_reservations(timestamptz, integer)', 'EXECUTE'),
    true,
    'service_role can execute purge_admission_reservations'
);
SELECT is(
    has_function_privilege('service_role', 'public.purge_privacy_operations(timestamptz, integer)', 'EXECUTE'),
    true,
    'service_role can execute purge_privacy_operations'
);
SELECT is(
    has_function_privilege('service_role', 'public.purge_outbox_events(timestamptz, integer)', 'EXECUTE'),
    true,
    'service_role can execute purge_outbox_events'
);

SELECT is(
    has_function_privilege('anon', 'public.purge_admission_reservations(timestamptz, integer)', 'EXECUTE'),
    false,
    'anon cannot execute purge_admission_reservations'
);
SELECT is(
    has_function_privilege('authenticated', 'public.purge_admission_reservations(timestamptz, integer)', 'EXECUTE'),
    false,
    'authenticated cannot execute purge_admission_reservations'
);
SELECT is(
    has_function_privilege('public', 'public.purge_admission_reservations(timestamptz, integer)', 'EXECUTE'),
    false,
    'PUBLIC cannot execute purge_admission_reservations'
);
SELECT is(
    has_function_privilege('anon', 'public.purge_privacy_operations(timestamptz, integer)', 'EXECUTE'),
    false,
    'anon cannot execute purge_privacy_operations'
);
SELECT is(
    has_function_privilege('authenticated', 'public.purge_privacy_operations(timestamptz, integer)', 'EXECUTE'),
    false,
    'authenticated cannot execute purge_privacy_operations'
);
SELECT is(
    has_function_privilege('anon', 'public.purge_outbox_events(timestamptz, integer)', 'EXECUTE'),
    false,
    'anon cannot execute purge_outbox_events'
);
SELECT is(
    has_function_privilege('authenticated', 'public.purge_outbox_events(timestamptz, integer)', 'EXECUTE'),
    false,
    'authenticated cannot execute purge_outbox_events'
);

-- =================================================================
-- 4. Fail-closed validation of purge parameters
-- =================================================================
SELECT throws_ok(
    'SELECT public.purge_admission_reservations(NULL, 10)',
    NULL,
    'invalid retention parameters',
    'NULL cutoff fails closed'
);
SELECT throws_ok(
    'SELECT public.purge_admission_reservations(now(), 0)',
    NULL,
    'invalid retention parameters',
    'batch_size 0 fails closed'
);
SELECT throws_ok(
    'SELECT public.purge_admission_reservations(now(), 1001)',
    NULL,
    'invalid retention parameters',
    'batch_size 1001 fails closed'
);
SELECT throws_ok(
    'SELECT public.purge_privacy_operations(now(), NULL)',
    NULL,
    'invalid retention parameters',
    'NULL batch_size fails closed'
);
SELECT throws_ok(
    'SELECT public.purge_outbox_events(now(), -5)',
    NULL,
    'invalid retention parameters',
    'negative batch_size fails closed'
);

-- =================================================================
-- 5. admission_reservations purge semantics
-- =================================================================
-- Expired rows (older than cutoff) are removed; current rows stay.
INSERT INTO public.admission_reservations
    (user_id, request_id, message_hmac_sha256, network_hmac_sha256,
     estimated_units, reserved_at)
VALUES
    ('ret-adm-expired', '11111111-1111-1111-1111-111111111111',
     repeat('a', 64), repeat('b', 64), 10,
     now() - interval '25 hours'),
    ('ret-adm-current', '22222222-2222-2222-2222-222222222222',
     repeat('c', 64), repeat('d', 64), 10,
     now() - interval '23 hours');

SELECT is(
    public.purge_admission_reservations(now() - interval '24 hours', 100),
    1,
    'purge_admission_reservations removes exactly the expired row'
);
SELECT is(
    (SELECT count(*)::integer FROM public.admission_reservations WHERE user_id = 'ret-adm-expired'),
    0,
    'expired admission reservation is gone'
);
SELECT is(
    (SELECT count(*)::integer FROM public.admission_reservations WHERE user_id = 'ret-adm-current'),
    1,
    'current admission reservation stays (quota ledger preserved)'
);
SELECT is(
    (SELECT count(*)::integer FROM public.admission_reservations WHERE user_id = 'ret-adm-current'),
    1,
    'purge never touches rows inside the horizon'
);

-- =================================================================
-- 6. privacy_operations purge semantics
-- =================================================================
INSERT INTO public.privacy_operations
    (user_id, operation_id, operation, operation_payload_sha256, status,
     applied_at, result)
VALUES
    ('ret-prv-expired', '11111111-1111-1111-1111-111111111111',
     'delete_history', repeat('e', 64), 'applied',
     now() - interval '31 days', '{"status":"applied"}'::jsonb),
    ('ret-prv-current', '22222222-2222-2222-2222-222222222222',
     'delete_memories', repeat('f', 64), 'applied',
     now() - interval '29 days', '{"status":"applied"}'::jsonb);

SELECT is(
    public.purge_privacy_operations(now() - interval '30 days', 100),
    1,
    'purge_privacy_operations removes exactly the expired ledger row'
);
SELECT is(
    (SELECT count(*)::integer FROM public.privacy_operations WHERE user_id = 'ret-prv-expired'),
    0,
    'expired privacy ledger row is gone'
);
SELECT is(
    (SELECT count(*)::integer FROM public.privacy_operations WHERE user_id = 'ret-prv-current'),
    1,
    'privacy ledger row inside the 30-day horizon stays'
);

-- =================================================================
-- 7. outbox_events purge semantics
-- =================================================================
-- outbox_events has an FK to profiles(user_id); seed the profile first.
INSERT INTO public.profiles
    (user_id, persona_config, user_profile, relationship_state,
     emotional_state, revision)
VALUES (
    'ret-outbox', 'persona', '{}'::jsonb,
    '{"schema_version":1,"trust":0.5,"affection":0.3,"tension":0.0,"triggers":[],"timestamp":1700000000.0}'::jsonb,
    '{"schema_version":1,"pleasure":0.0,"arousal":0.0,"dominance":0.0,"libido":0.0,"aggression":0.0,"connection":0.5,"energy":0.8,"tension":0.0,"coping_mode":"HEALTHY","timestamp":1700000000.0}'::jsonb,
    1
);

-- Completed / dead_letter with expired retention_until are removed.
INSERT INTO public.outbox_events
    (id, event_type, contract_version, user_id, turn_request_id, payload,
     status, attempts, next_attempt_at, lease_owner, lease_expires_at,
     idempotency_key, error_code, created_at, updated_at, processed_at,
     dead_lettered_at, retention_until)
VALUES
    ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'turn_completed', 1,
     'ret-outbox', NULL, '{"ref":"a"}'::jsonb,
     'completed', 1, NULL, NULL, NULL, 'ret-outbox-k-completed', NULL,
     now(), now(), now() - interval '2 days', NULL,
     now() - interval '1 day'),
    ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'turn_completed', 1,
     'ret-outbox', NULL, '{"ref":"b"}'::jsonb,
     'dead_letter', 10, NULL, NULL, NULL, 'ret-outbox-k-dead', 'delivery_failed',
     now(), now(), NULL, now() - interval '2 days',
     now() - interval '1 day'),
    ('cccccccc-cccc-cccc-cccc-cccccccccccc', 'turn_completed', 1,
     'ret-outbox', NULL, '{"ref":"c"}'::jsonb,
     'completed', 1, NULL, NULL, NULL, 'ret-outbox-k-completed-future', NULL,
     now(), now(), now() - interval '2 days', NULL,
     now() + interval '1 day');

SELECT is(
    public.purge_outbox_events(now(), 100),
    2,
    'purge_outbox_events removes final events with expired retention_until'
);
SELECT is(
    (SELECT count(*)::integer FROM public.outbox_events WHERE id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'),
    0,
    'expired completed event is gone'
);
SELECT is(
    (SELECT count(*)::integer FROM public.outbox_events WHERE id = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'),
    0,
    'expired dead_letter event is gone'
);
SELECT is(
    (SELECT count(*)::integer FROM public.outbox_events WHERE id = 'cccccccc-cccc-cccc-cccc-cccccccccccc'),
    1,
    'final event with future retention_until stays'
);

-- Active states are NEVER purged by age, even with an old retention_until.
INSERT INTO public.outbox_events
    (id, event_type, contract_version, user_id, turn_request_id, payload,
     status, attempts, next_attempt_at, lease_owner, lease_expires_at,
     idempotency_key, error_code, created_at, updated_at, processed_at,
     dead_lettered_at, retention_until)
VALUES
    ('dddddddd-dddd-dddd-dddd-dddddddddddd', 'turn_completed', 1,
     'ret-outbox', NULL, '{"ref":"d"}'::jsonb,
     'pending', 0, now() - interval '40 days', NULL, NULL,
     'ret-outbox-k-pending', NULL,
     now() - interval '40 days', now() - interval '40 days', NULL, NULL, NULL),
    ('eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee', 'turn_completed', 1,
     'ret-outbox', NULL, '{"ref":"e"}'::jsonb,
     'processing', 1, NULL, 'worker-a', now() - interval '40 days',
     'ret-outbox-k-processing', NULL,
     now() - interval '40 days', now() - interval '40 days', NULL, NULL, NULL),
    ('ffffffff-ffff-ffff-ffff-ffffffffffff', 'turn_completed', 1,
     'ret-outbox', NULL, '{"ref":"f"}'::jsonb,
     'failed', 2, now() - interval '40 days', NULL, NULL,
     'ret-outbox-k-failed', 'delivery_failed',
     now() - interval '40 days', now() - interval '40 days', NULL, NULL, NULL);

SELECT is(
    public.purge_outbox_events(now(), 100),
    0,
    'active outbox states are never purged by age'
);
SELECT is(
    (SELECT count(*)::integer FROM public.outbox_events WHERE status IN ('pending', 'processing', 'failed')),
    3,
    'pending/processing/failed events remain after purge'
);

-- =================================================================
-- 8. Batch size is bounded
-- =================================================================
-- Cutoff is now - 24h so only this section's 2-day-old rows are eligible
-- (the earlier 23h-old current reservation must stay untouched).
INSERT INTO public.admission_reservations
    (user_id, request_id, message_hmac_sha256, network_hmac_sha256,
     estimated_units, reserved_at)
SELECT
    'ret-batch', gen_random_uuid(), repeat('a', 64), repeat('b', 64), 10,
    now() - interval '2 days'
FROM generate_series(1, 5);

SELECT is(
    public.purge_admission_reservations(now() - interval '24 hours', 2),
    2,
    'purge removes at most batch_size rows per call'
);
SELECT is(
    public.purge_admission_reservations(now() - interval '24 hours', 2),
    2,
    'second batch removes the next batch'
);
SELECT is(
    public.purge_admission_reservations(now() - interval '24 hours', 2),
    1,
    'final partial batch removes the remainder'
);
SELECT is(
    (SELECT count(*)::integer FROM public.admission_reservations WHERE user_id = 'ret-batch'),
    0,
    'all expired batch rows are eventually removed'
);

-- =================================================================
-- 9. Future cutoff cannot advance deletion (DB-authoritative clamp)
-- =================================================================
-- The purge RPCs clamp the caller-supplied cutoff against authoritative
-- PostgreSQL time (clock_timestamp()): a future cutoff must never remove
-- rows inside the binding minimum horizons, and the genuinely expired
-- rows remain removable.
INSERT INTO public.admission_reservations
    (user_id, request_id, message_hmac_sha256, network_hmac_sha256,
     estimated_units, reserved_at)
VALUES
    ('ret-adm-future-expired', '33333333-3333-3333-3333-333333333333',
     repeat('a', 64), repeat('b', 64), 10,
     now() - interval '25 hours'),
    ('ret-adm-future-current', '44444444-4444-4444-4444-444444444444',
     repeat('c', 64), repeat('d', 64), 10,
     now() - interval '23 hours');

SELECT is(
    public.purge_admission_reservations(now() + interval '1 day', 100),
    1,
    'future cutoff removes only the admission row older than 24h'
);
SELECT is(
    (SELECT count(*)::integer FROM public.admission_reservations WHERE user_id = 'ret-adm-future-current'),
    1,
    'admission row younger than 24h survives a future cutoff'
);
SELECT is(
    (SELECT count(*)::integer FROM public.admission_reservations WHERE user_id = 'ret-adm-future-expired'),
    0,
    'admission row older than 24h is still removable under a future cutoff'
);

INSERT INTO public.privacy_operations
    (user_id, operation_id, operation, operation_payload_sha256, status,
     applied_at, result)
VALUES
    ('ret-prv-future-expired', '33333333-3333-3333-3333-333333333333',
     'delete_history', repeat('e', 64), 'applied',
     now() - interval '31 days', '{"status":"applied"}'::jsonb),
    ('ret-prv-future-current', '44444444-4444-4444-4444-444444444444',
     'delete_memories', repeat('f', 64), 'applied',
     now() - interval '29 days', '{"status":"applied"}'::jsonb);

SELECT is(
    public.purge_privacy_operations(now() + interval '1 day', 100),
    1,
    'future cutoff removes only the privacy ledger row older than 30 days'
);
SELECT is(
    (SELECT count(*)::integer FROM public.privacy_operations WHERE user_id = 'ret-prv-future-current'),
    1,
    'privacy ledger row younger than 30 days survives a future cutoff'
);
SELECT is(
    (SELECT count(*)::integer FROM public.privacy_operations WHERE user_id = 'ret-prv-future-expired'),
    0,
    'privacy ledger row older than 30 days is still removable under a future cutoff'
);

-- Outbox: FK to profiles(user_id); seed the profile first.
INSERT INTO public.profiles
    (user_id, persona_config, user_profile, relationship_state,
     emotional_state, revision)
VALUES (
    'ret-outbox-future', 'persona', '{}'::jsonb,
    '{"schema_version":1,"trust":0.5,"affection":0.3,"tension":0.0,"triggers":[],"timestamp":1700000000.0}'::jsonb,
    '{"schema_version":1,"pleasure":0.0,"arousal":0.0,"dominance":0.0,"libido":0.0,"aggression":0.0,"connection":0.5,"energy":0.8,"tension":0.0,"coping_mode":"HEALTHY","timestamp":1700000000.0}'::jsonb,
    1
);

INSERT INTO public.outbox_events
    (id, event_type, contract_version, user_id, turn_request_id, payload,
     status, attempts, next_attempt_at, lease_owner, lease_expires_at,
     idempotency_key, error_code, created_at, updated_at, processed_at,
     dead_lettered_at, retention_until)
VALUES
    ('bbbbbbbb-cccc-cccc-cccc-cccccccccccc', 'turn_completed', 1,
     'ret-outbox-future', NULL, '{"ref":"g"}'::jsonb,
     'completed', 1, NULL, NULL, NULL, 'ret-outbox-f-k-expired', NULL,
     now(), now(), now() - interval '2 days', NULL,
     now() - interval '1 day'),
    ('bbbbbbbb-eeee-eeee-eeee-eeeeeeeeeeee', 'turn_completed', 1,
     'ret-outbox-future', NULL, '{"ref":"h"}'::jsonb,
     'completed', 1, NULL, NULL, NULL, 'ret-outbox-f-k-future', NULL,
     now(), now(), now() - interval '2 days', NULL,
     now() + interval '1 day');

SELECT is(
    public.purge_outbox_events(now() + interval '1 day', 100),
    1,
    'future cutoff removes only the outbox event with expired retention_until'
);
SELECT is(
    (SELECT count(*)::integer FROM public.outbox_events WHERE id = 'bbbbbbbb-eeee-eeee-eeee-eeeeeeeeeeee'),
    1,
    'outbox event with future retention_until survives a future cutoff'
);
SELECT is(
    (SELECT count(*)::integer FROM public.outbox_events WHERE id = 'bbbbbbbb-cccc-cccc-cccc-cccccccccccc'),
    0,
    'outbox event with expired retention_until is still removable under a future cutoff'
);

-- =================================================================
-- 10. Cleanup never attaches to user-controlled tables
-- =================================================================
-- The retention migration must not attach any purge trigger to
-- user-controlled tables (chat_logs, memories, archival_extractions,
-- turn_requests, profiles): those tables keep NO automatic TTL.
SELECT is(
    (SELECT count(*)::integer FROM pg_trigger t
      JOIN pg_class c ON c.oid = t.tgrelid
      JOIN pg_namespace n ON n.oid = c.relnamespace
      JOIN pg_proc p ON p.oid = t.tgfoid
     WHERE n.nspname = 'public'
       AND c.relname IN ('chat_logs', 'memories', 'archival_extractions',
                         'turn_requests', 'profiles')
       AND p.proname IN ('purge_admission_reservations',
                         'purge_privacy_operations',
                         'purge_outbox_events')
       AND NOT t.tgisinternal),
    0,
    'retention purge functions are never attached as triggers to user tables'
);

SELECT finish();
ROLLBACK;
