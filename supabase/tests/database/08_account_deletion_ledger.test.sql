BEGIN;

CREATE EXTENSION IF NOT EXISTS pgtap;
SELECT plan(114);

-- =================================================================
-- 1. Table exists with the exact columns
-- =================================================================
SELECT has_table('public', 'account_deletion_jobs', 'account_deletion_jobs exists');

SELECT is(
    (SELECT array_agg(attname ORDER BY attnum)::text
       FROM pg_attribute
      WHERE attrelid = 'public.account_deletion_jobs'::regclass
        AND attnum > 0
        AND NOT attisdropped),
    ARRAY[
        'job_id', 'operation_id', 'user_id', 'user_ref_hmac_sha256',
        'intent_fingerprint_sha256', 'status', 'attempts', 'lease_owner',
        'lease_expires_at', 'next_attempt_at', 'db_purged_at', 'error_code',
        'requested_at', 'updated_at', 'completed_at'
    ]::text,
    'account_deletion_jobs has the exact column set'
);

-- =================================================================
-- 2. Exact constraints
-- =================================================================
SELECT is(
    (SELECT array_agg(conname ORDER BY conname)::text
       FROM pg_constraint
      WHERE conrelid = 'public.account_deletion_jobs'::regclass),
    ARRAY[
        'account_deletion_jobs_attempts_check',
        'account_deletion_jobs_completed_at_state_check',
        'account_deletion_jobs_db_purged_state_check',
        'account_deletion_jobs_error_code_check',
        'account_deletion_jobs_failed_error_check',
        'account_deletion_jobs_fingerprint_check',
        'account_deletion_jobs_idempotency_key',
        'account_deletion_jobs_lease_owner_check',
        'account_deletion_jobs_lease_pair_check',
        'account_deletion_jobs_lease_state_check',
        'account_deletion_jobs_pkey',
        'account_deletion_jobs_status_check',
        'account_deletion_jobs_user_id_check',
        'account_deletion_jobs_user_id_state_check',
        'account_deletion_jobs_user_ref_check'
    ]::text,
    'account_deletion_jobs has the exact constraint set'
);

-- =================================================================
-- 3. Required indexes
-- =================================================================
SELECT ok(
    EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relname = 'account_deletion_jobs_status_next_attempt_idx'
    ),
    'account_deletion_jobs_status_next_attempt_idx exists'
);
SELECT ok(
    EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relname = 'account_deletion_jobs_user_ref_idx'
    ),
    'account_deletion_jobs_user_ref_idx exists'
);
SELECT ok(
    EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relname = 'account_deletion_jobs_completed_at_idx'
    ),
    'account_deletion_jobs_completed_at_idx exists'
);

-- =================================================================
-- 4. No FK to profiles
-- =================================================================
SELECT is(
    (SELECT count(*)::integer
       FROM pg_constraint con
       JOIN pg_class rel ON rel.oid = con.conrelid
       JOIN pg_namespace n ON n.oid = rel.relnamespace
      WHERE n.nspname = 'public'
        AND rel.relname = 'account_deletion_jobs'
        AND con.contype = 'f'
        AND con.confrelid = 'public.profiles'::regclass),
    0,
    'account_deletion_jobs has no FK to profiles'
);

-- =================================================================
-- 5/6/7. RLS, FORCE RLS, zero policies
-- =================================================================
SELECT ok(
    (SELECT relrowsecurity FROM pg_class WHERE oid = 'public.account_deletion_jobs'::regclass),
    'RLS is enabled on account_deletion_jobs'
);
SELECT ok(
    (SELECT relforcerowsecurity FROM pg_class WHERE oid = 'public.account_deletion_jobs'::regclass),
    'FORCE RLS is enabled on account_deletion_jobs'
);
SELECT is(
    (SELECT count(*)::integer FROM pg_policies
      WHERE schemaname = 'public' AND tablename = 'account_deletion_jobs'),
    0,
    'account_deletion_jobs has zero policies'
);

-- =================================================================
-- 8. No runtime role has direct table access
-- =================================================================
SELECT ok(
    NOT has_table_privilege('service_role', 'public.account_deletion_jobs',
        'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER'),
    'service_role has no table privileges'
);
SELECT ok(
    NOT has_table_privilege('anon', 'public.account_deletion_jobs',
        'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER'),
    'anon has no table privileges'
);
SELECT ok(
    NOT has_table_privilege('authenticated', 'public.account_deletion_jobs',
        'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER'),
    'authenticated has no table privileges'
);
SELECT ok(
    NOT has_table_privilege('public', 'public.account_deletion_jobs',
        'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER'),
    'PUBLIC has no table privileges'
);

-- =================================================================
-- 9/10. RPC grants exact; internal helpers not exposed
-- =================================================================
-- Runtime RPCs: exist, SECURITY DEFINER, search_path = '', service_role
-- only. One ok() per RPC per property via unnest.
SELECT ok(
    EXISTS (
        SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND p.proname = rpc
    ),
    format('%s exists', rpc)
) FROM unnest(ARRAY[
    'account_deletion_request', 'account_deletion_has_tombstone',
    'account_deletion_acquire_lease', 'account_deletion_purge',
    'account_deletion_record_failure', 'account_deletion_record_retry',
    'account_deletion_finalize', 'account_deletion_purge_completed'
]) AS rpc;

SELECT ok(
    (SELECT prosecdef FROM pg_proc WHERE oid = format('public.%s(%s)', rpc, sig)::regprocedure),
    format('%s is SECURITY DEFINER', rpc)
) FROM (VALUES
    ('account_deletion_request', 'text, text, uuid, text'),
    ('account_deletion_has_tombstone', 'text'),
    ('account_deletion_acquire_lease', 'text, integer, integer'),
    ('account_deletion_purge', 'uuid, text, text'),
    ('account_deletion_record_failure', 'uuid, text, text'),
    ('account_deletion_record_retry', 'uuid, text'),
    ('account_deletion_finalize', 'uuid, text'),
    ('account_deletion_purge_completed', 'timestamptz, integer')
) AS t(rpc, sig);

SELECT ok(
    (SELECT p.proconfig @> ARRAY['search_path=""'] FROM pg_proc p WHERE p.oid = format('public.%s(%s)', rpc, sig)::regprocedure),
    format('%s has an empty search_path', rpc)
) FROM (VALUES
    ('account_deletion_request', 'text, text, uuid, text'),
    ('account_deletion_has_tombstone', 'text'),
    ('account_deletion_acquire_lease', 'text, integer, integer'),
    ('account_deletion_purge', 'uuid, text, text'),
    ('account_deletion_record_failure', 'uuid, text, text'),
    ('account_deletion_record_retry', 'uuid, text'),
    ('account_deletion_finalize', 'uuid, text'),
    ('account_deletion_purge_completed', 'timestamptz, integer')
) AS t(rpc, sig);

SELECT ok(
    has_function_privilege('service_role', format('public.%s(%s)', rpc, sig), 'EXECUTE'),
    format('service_role can execute %s', rpc)
) FROM (VALUES
    ('account_deletion_request', 'text, text, uuid, text'),
    ('account_deletion_has_tombstone', 'text'),
    ('account_deletion_acquire_lease', 'text, integer, integer'),
    ('account_deletion_purge', 'uuid, text, text'),
    ('account_deletion_record_failure', 'uuid, text, text'),
    ('account_deletion_record_retry', 'uuid, text'),
    ('account_deletion_finalize', 'uuid, text'),
    ('account_deletion_purge_completed', 'timestamptz, integer')
) AS t(rpc, sig);

SELECT ok(
    NOT has_function_privilege('anon', format('public.%s(%s)', rpc, sig), 'EXECUTE'),
    format('anon cannot execute %s', rpc)
) FROM (VALUES
    ('account_deletion_request', 'text, text, uuid, text'),
    ('account_deletion_has_tombstone', 'text'),
    ('account_deletion_acquire_lease', 'text, integer, integer'),
    ('account_deletion_purge', 'uuid, text, text'),
    ('account_deletion_record_failure', 'uuid, text, text'),
    ('account_deletion_record_retry', 'uuid, text'),
    ('account_deletion_finalize', 'uuid, text'),
    ('account_deletion_purge_completed', 'timestamptz, integer')
) AS t(rpc, sig);

SELECT ok(
    NOT has_function_privilege('authenticated', format('public.%s(%s)', rpc, sig), 'EXECUTE'),
    format('authenticated cannot execute %s', rpc)
) FROM (VALUES
    ('account_deletion_request', 'text, text, uuid, text'),
    ('account_deletion_has_tombstone', 'text'),
    ('account_deletion_acquire_lease', 'text, integer, integer'),
    ('account_deletion_purge', 'uuid, text, text'),
    ('account_deletion_record_failure', 'uuid, text, text'),
    ('account_deletion_record_retry', 'uuid, text'),
    ('account_deletion_finalize', 'uuid, text'),
    ('account_deletion_purge_completed', 'timestamptz, integer')
) AS t(rpc, sig);

SELECT ok(
    NOT has_function_privilege('public', format('public.%s(%s)', rpc, sig), 'EXECUTE'),
    format('PUBLIC cannot execute %s', rpc)
) FROM (VALUES
    ('account_deletion_request', 'text, text, uuid, text'),
    ('account_deletion_has_tombstone', 'text'),
    ('account_deletion_acquire_lease', 'text, integer, integer'),
    ('account_deletion_purge', 'uuid, text, text'),
    ('account_deletion_record_failure', 'uuid, text, text'),
    ('account_deletion_record_retry', 'uuid, text'),
    ('account_deletion_finalize', 'uuid, text'),
    ('account_deletion_purge_completed', 'timestamptz, integer')
) AS t(rpc, sig);

-- Internal helpers exist but carry NO runtime grants.
SELECT ok(
    EXISTS (
        SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND p.proname = helper
    ),
    format('%s exists', helper)
) FROM unnest(ARRAY[
    'account_deletion_validation_error',
    'account_deletion_assert_owner',
    'account_deletion_intent_fingerprint_sha256'
]) AS helper;

SELECT ok(
    NOT has_function_privilege('service_role', format('public.%s(%s)', helper, sig), 'EXECUTE'),
    format('service_role cannot execute internal %s', helper)
) FROM (VALUES
    ('account_deletion_validation_error', 'text, text, uuid, text'),
    ('account_deletion_assert_owner', 'uuid, text'),
    ('account_deletion_intent_fingerprint_sha256', 'jsonb')
) AS t(helper, sig);

SELECT ok(
    NOT has_function_privilege('public', format('public.%s(%s)', helper, sig), 'EXECUTE'),
    format('PUBLIC cannot execute internal %s', helper)
) FROM (VALUES
    ('account_deletion_validation_error', 'text, text, uuid, text'),
    ('account_deletion_assert_owner', 'uuid, text'),
    ('account_deletion_intent_fingerprint_sha256', 'jsonb')
) AS t(helper, sig);

-- =================================================================
-- 11/12/13/14. Constraints reject incoherent states and bad values
-- =================================================================
-- pending must not carry a lease
SELECT throws_ok(
    'INSERT INTO public.account_deletion_jobs
        (operation_id, user_id, user_ref_hmac_sha256, intent_fingerprint_sha256,
         status, lease_owner, lease_expires_at)
     VALUES (gen_random_uuid(), ''u-1'', repeat(''a'', 64), repeat(''b'', 64),
         ''pending'', ''worker-1'', now() + interval ''1 minute'')',
    NULL, NULL,
    'pending with a lease is rejected'
);
-- processing must carry a lease
SELECT throws_ok(
    'INSERT INTO public.account_deletion_jobs
        (operation_id, user_id, user_ref_hmac_sha256, intent_fingerprint_sha256, status)
     VALUES (gen_random_uuid(), ''u-1'', repeat(''a'', 64), repeat(''b'', 64), ''processing'')',
    NULL, NULL,
    'processing without a lease is rejected'
);
-- lease owner without expiry is rejected
SELECT throws_ok(
    'INSERT INTO public.account_deletion_jobs
        (operation_id, user_id, user_ref_hmac_sha256, intent_fingerprint_sha256,
         status, lease_owner)
     VALUES (gen_random_uuid(), ''u-1'', repeat(''a'', 64), repeat(''b'', 64),
         ''processing'', ''worker-1'')',
    NULL, NULL,
    'lease owner without expiry is rejected'
);
-- completed requires db_purged_at and completed_at
SELECT throws_ok(
    'INSERT INTO public.account_deletion_jobs
        (operation_id, user_id, user_ref_hmac_sha256, intent_fingerprint_sha256,
         status, completed_at)
     VALUES (gen_random_uuid(), ''u-1'', repeat(''a'', 64), repeat(''b'', 64),
         ''completed'', now())',
    NULL, NULL,
    'completed without db_purged_at is rejected'
);
-- completed must not preserve the raw user_id
SELECT throws_ok(
    'INSERT INTO public.account_deletion_jobs
        (operation_id, user_id, user_ref_hmac_sha256, intent_fingerprint_sha256,
         status, db_purged_at, completed_at)
     VALUES (gen_random_uuid(), ''u-1'', repeat(''a'', 64), repeat(''b'', 64),
         ''completed'', now(), now())',
    NULL, NULL,
    'completed with a raw user_id is rejected (identity minimization)'
);
-- failed requires a sanitized error_code
SELECT throws_ok(
    'INSERT INTO public.account_deletion_jobs
        (operation_id, user_id, user_ref_hmac_sha256, intent_fingerprint_sha256, status)
     VALUES (gen_random_uuid(), ''u-1'', repeat(''a'', 64), repeat(''b'', 64), ''failed'')',
    NULL, NULL,
    'failed without error_code is rejected'
);
-- attempts can never be negative
SELECT throws_ok(
    'INSERT INTO public.account_deletion_jobs
        (operation_id, user_id, user_ref_hmac_sha256, intent_fingerprint_sha256,
         status, attempts)
     VALUES (gen_random_uuid(), ''u-1'', repeat(''a'', 64), repeat(''b'', 64),
         ''pending'', -1)',
    NULL, NULL,
    'negative attempts are rejected'
);
-- unknown status is rejected
SELECT throws_ok(
    'INSERT INTO public.account_deletion_jobs
        (operation_id, user_id, user_ref_hmac_sha256, intent_fingerprint_sha256, status)
     VALUES (gen_random_uuid(), ''u-1'', repeat(''a'', 64), repeat(''b'', 64), ''exploded'')',
    NULL, NULL,
    'unknown status is rejected'
);
-- lease_owner must match the strict allowlist
SELECT throws_ok(
    'INSERT INTO public.account_deletion_jobs
        (operation_id, user_id, user_ref_hmac_sha256, intent_fingerprint_sha256,
         status, lease_owner, lease_expires_at)
     VALUES (gen_random_uuid(), ''u-1'', repeat(''a'', 64), repeat(''b'', 64),
         ''processing'', ''bad owner!!'', now() + interval ''1 minute'')',
    NULL, NULL,
    'lease_owner outside the allowlist is rejected'
);
-- error_code must match the strict allowlist
SELECT throws_ok(
    'INSERT INTO public.account_deletion_jobs
        (operation_id, user_id, user_ref_hmac_sha256, intent_fingerprint_sha256,
         status, error_code)
     VALUES (gen_random_uuid(), ''u-1'', repeat(''a'', 64), repeat(''b'', 64),
         ''failed'', ''Bad Code'')',
    NULL, NULL,
    'error_code outside the allowlist is rejected'
);
-- invalid HMAC reference is rejected
SELECT throws_ok(
    'INSERT INTO public.account_deletion_jobs
        (operation_id, user_id, user_ref_hmac_sha256, intent_fingerprint_sha256, status)
     VALUES (gen_random_uuid(), ''u-1'', ''not-hex'', repeat(''b'', 64), ''pending'')',
    NULL, NULL,
    'invalid user_ref_hmac_sha256 is rejected'
);
-- invalid intent fingerprint is rejected
SELECT throws_ok(
    'INSERT INTO public.account_deletion_jobs
        (operation_id, user_id, user_ref_hmac_sha256, intent_fingerprint_sha256, status)
     VALUES (gen_random_uuid(), ''u-1'', repeat(''a'', 64), ''XYZ'', ''pending'')',
    NULL, NULL,
    'invalid intent_fingerprint_sha256 is rejected'
);
-- empty/whitespace user_id is rejected while active
SELECT throws_ok(
    'INSERT INTO public.account_deletion_jobs
        (operation_id, user_id, user_ref_hmac_sha256, intent_fingerprint_sha256, status)
     VALUES (gen_random_uuid(), ''   '', repeat(''a'', 64), repeat(''b'', 64), ''pending'')',
    NULL, NULL,
    'whitespace user_id is rejected'
);
-- duplicate (ref, operation_id) is rejected by the idempotency key
SELECT throws_ok(
    'INSERT INTO public.account_deletion_jobs
        (operation_id, user_id, user_ref_hmac_sha256, intent_fingerprint_sha256, status)
     VALUES (''11111111-1111-1111-1111-111111111111''::uuid, ''u-1'',
         repeat(''a'', 64), repeat(''b'', 64), ''pending''),
        (''11111111-1111-1111-1111-111111111111''::uuid, ''u-2'',
         repeat(''a'', 64), repeat(''b'', 64), ''pending'')',
    NULL, NULL,
    'duplicate (ref, operation_id) is rejected (idempotency key)'
);

-- =================================================================
-- Functional flow: request -> replay -> conflict -> claim -> purge ->
-- finalize -> retention
-- =================================================================
INSERT INTO public.profiles (user_id, persona_config, user_profile,
    relationship_state, emotional_state, revision)
VALUES (
    'adl-pgtap', 'persona', '{}'::jsonb,
    '{"schema_version":1,"trust":0.5,"affection":0.3,"tension":0.0,"triggers":[],"timestamp":1700000000.0}'::jsonb,
    '{"schema_version":1,"pleasure":0.0,"arousal":0.0,"dominance":0.0,"libido":0.0,"aggression":0.0,"connection":0.5,"energy":0.8,"tension":0.0,"coping_mode":"HEALTHY","timestamp":1700000000.0}'::jsonb,
    1
);
INSERT INTO public.chat_logs (user_id, role, content)
VALUES ('adl-pgtap', 'user', 'hi'), ('adl-pgtap', 'assistant', 'hello');
INSERT INTO public.memories (user_id, content, metadata)
VALUES ('adl-pgtap', 'memory', '{}'::jsonb);
INSERT INTO public.admission_reservations
    (user_id, request_id, message_hmac_sha256, network_hmac_sha256, estimated_units)
VALUES ('adl-pgtap', gen_random_uuid(), repeat('a', 64), repeat('b', 64), 10);
INSERT INTO public.privacy_operations
    (user_id, operation_id, operation, operation_payload_sha256, status, result)
VALUES ('adl-pgtap', gen_random_uuid(), 'delete_history', repeat('c', 64), 'applied', '{}'::jsonb);

-- A valid, coherent job is accepted.
INSERT INTO public.account_deletion_jobs (
    operation_id, user_id, user_ref_hmac_sha256, intent_fingerprint_sha256,
    status, attempts, next_attempt_at
) VALUES (
    '22222222-2222-2222-2222-222222222222'::uuid, 'adl-pgtap',
    repeat('d', 64), repeat('e', 64), 'pending', 0, clock_timestamp()
);

-- Request RPC: replay returns the existing job; divergent fingerprint
-- conflicts; tombstone lookup works.
SELECT is(
    (public.account_deletion_request('adl-pgtap', repeat('d', 64),
        '22222222-2222-2222-2222-222222222222'::uuid, repeat('e', 64))->>'status'),
    'replay',
    'request replays the same ref+operation_id+fingerprint'
);
SELECT is(
    (public.account_deletion_request('adl-pgtap', repeat('d', 64),
        '22222222-2222-2222-2222-222222222222'::uuid, repeat('f', 64))->'error'->>'code'),
    'operation_conflict',
    'divergent fingerprint produces a structured conflict'
);
SELECT is(
    (public.account_deletion_has_tombstone(repeat('d', 64))->>'exists'),
    'true',
    'tombstone lookup reports the blocking job'
);

-- Claim: first claim wins; a second claim finds nothing.
SELECT is(
    (public.account_deletion_acquire_lease('worker-pg', 60, 100)->>'found'),
    'true',
    'first claim acquires the job'
);
SELECT is(
    (public.account_deletion_acquire_lease('worker-pg2', 60, 100)->>'found'),
    'false',
    'second concurrent claim finds no eligible job'
);

-- Old worker without the lease cannot purge (fails closed, sanitized).
SELECT throws_ok(
    'SELECT public.account_deletion_purge(
        (SELECT job_id FROM public.account_deletion_jobs
          WHERE user_ref_hmac_sha256 = repeat(''d'', 64)),
        ''not-the-owner'', repeat(''e'', 64))',
    'P0001', 'persistence error',
    'purge from a non-owner fails closed with a persistence error'
);

-- Owner purges: every table of the user is removed in one transaction.
SELECT is(
    (public.account_deletion_purge(
        (SELECT job_id FROM public.account_deletion_jobs
          WHERE user_ref_hmac_sha256 = repeat('d', 64)),
        'worker-pg', repeat('e', 64))->>'status'),
    'purged',
    'owner purge completes'
);
SELECT is(
    (SELECT count(*)::integer FROM public.chat_logs WHERE user_id = 'adl-pgtap'),
    0,
    'chat_logs purged'
);
SELECT is(
    (SELECT count(*)::integer FROM public.memories WHERE user_id = 'adl-pgtap'),
    0,
    'memories purged'
);
SELECT is(
    (SELECT count(*)::integer FROM public.admission_reservations WHERE user_id = 'adl-pgtap'),
    0,
    'admission_reservations purged'
);
SELECT is(
    (SELECT count(*)::integer FROM public.privacy_operations WHERE user_id = 'adl-pgtap'),
    0,
    'privacy_operations purged'
);
SELECT is(
    (SELECT count(*)::integer FROM public.profiles WHERE user_id = 'adl-pgtap'),
    0,
    'profiles purged (tombstone survives)'
);
SELECT is(
    (SELECT count(*)::integer FROM public.account_deletion_jobs
      WHERE user_ref_hmac_sha256 = repeat('d', 64)),
    1,
    'tombstone survives the profiles purge'
);

-- Repeated purge after the data is gone is a safe replay.
SELECT is(
    (public.account_deletion_purge(
        (SELECT job_id FROM public.account_deletion_jobs
          WHERE user_ref_hmac_sha256 = repeat('d', 64)),
        'worker-pg', repeat('e', 64))->>'status'),
    'already_purged',
    'repeated purge on an empty database is a safe replay'
);

-- Finalize minimizes identity: user_id becomes NULL, ref persists.
SELECT is(
    (public.account_deletion_finalize(
        (SELECT job_id FROM public.account_deletion_jobs
          WHERE user_ref_hmac_sha256 = repeat('d', 64)),
        'worker-pg')->>'status'),
    'completed',
    'finalize completes the job'
);
SELECT is(
    (SELECT user_id IS NULL FROM public.account_deletion_jobs
      WHERE user_ref_hmac_sha256 = repeat('d', 64)),
    true,
    'completed job minimizes the raw user_id to NULL'
);
SELECT is(
    (SELECT count(*)::integer FROM public.account_deletion_jobs
      WHERE user_ref_hmac_sha256 = repeat('d', 64)
        AND status = 'completed'),
    1,
    'completed tombstone keeps the sanitized HMAC reference'
);
SELECT is(
    (public.account_deletion_has_tombstone(repeat('d', 64))->>'status'),
    'completed',
    'tombstone lookup still works after completion'
);

-- Retention: a recent completed tombstone is retained; an old completed
-- tombstone is removed; active/failed jobs are never removed by age.
SELECT is(
    public.account_deletion_purge_completed(now() - interval '10 days', 10),
    0,
    'completed tombstone younger than the horizon is retained'
);

-- An old completed tombstone (identity already minimized) is eligible.
INSERT INTO public.account_deletion_jobs (
    operation_id, user_id, user_ref_hmac_sha256, intent_fingerprint_sha256,
    status, attempts, db_purged_at, completed_at, next_attempt_at,
    requested_at, updated_at
) VALUES (
    '44444444-4444-4444-4444-444444444444'::uuid, NULL,
    repeat('9', 64), repeat('8', 64), 'completed', 1,
    now() - interval '45 days', now() - interval '45 days',
    now() - interval '45 days', now() - interval '45 days',
    now() - interval '45 days'
);
SELECT is(
    public.account_deletion_purge_completed(now() - interval '40 days', 10),
    1,
    'completed tombstone older than the horizon is removed'
);

-- Active/failed jobs are never removed by age.
INSERT INTO public.account_deletion_jobs (
    operation_id, user_id, user_ref_hmac_sha256, intent_fingerprint_sha256,
    status, attempts, error_code, next_attempt_at, requested_at, updated_at
) VALUES (
    '33333333-3333-3333-3333-333333333333'::uuid, 'adl-active',
    repeat('1', 64), repeat('2', 64), 'failed', 3, 'auth_unavailable',
    now() - interval '40 days', now() - interval '40 days', now() - interval '40 days'
);
SELECT is(
    public.account_deletion_purge_completed(now() + interval '1 day', 10),
    0,
    'failed jobs are never removed by age'
);

SELECT finish();
ROLLBACK;
