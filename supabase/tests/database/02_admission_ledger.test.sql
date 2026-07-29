BEGIN;

CREATE EXTENSION IF NOT EXISTS pgtap;
SELECT plan(50);

-- =================================================================
-- 1. Table exists
-- =================================================================
SELECT has_table('public', 'admission_reservations', 'admission_reservations table exists');

-- =================================================================
-- 2. Column types and NOT NULL
-- =================================================================
SELECT has_column('public', 'admission_reservations', 'user_id', 'has user_id column');
SELECT col_not_null('public', 'admission_reservations', 'user_id', 'user_id is NOT NULL');
SELECT col_type_is('public', 'admission_reservations', 'user_id', 'text', 'user_id is text');

SELECT has_column('public', 'admission_reservations', 'request_id', 'has request_id column');
SELECT col_not_null('public', 'admission_reservations', 'request_id', 'request_id is NOT NULL');
SELECT col_type_is('public', 'admission_reservations', 'request_id', 'uuid', 'request_id is uuid');

SELECT has_column('public', 'admission_reservations', 'message_hmac_sha256', 'has message_hmac_sha256 column');
SELECT col_not_null('public', 'admission_reservations', 'message_hmac_sha256', 'message_hmac_sha256 is NOT NULL');
SELECT col_type_is('public', 'admission_reservations', 'message_hmac_sha256', 'text', 'message_hmac_sha256 is text');

SELECT has_column('public', 'admission_reservations', 'network_hmac_sha256', 'has network_hmac_sha256 column');
SELECT col_not_null('public', 'admission_reservations', 'network_hmac_sha256', 'network_hmac_sha256 is NOT NULL');
SELECT col_type_is('public', 'admission_reservations', 'network_hmac_sha256', 'text', 'network_hmac_sha256 is text');

SELECT has_column('public', 'admission_reservations', 'estimated_units', 'has estimated_units column');
SELECT col_not_null('public', 'admission_reservations', 'estimated_units', 'estimated_units is NOT NULL');
SELECT col_type_is('public', 'admission_reservations', 'estimated_units', 'integer', 'estimated_units is integer');

SELECT has_column('public', 'admission_reservations', 'reserved_at', 'has reserved_at column');
SELECT col_not_null('public', 'admission_reservations', 'reserved_at', 'reserved_at is NOT NULL');
SELECT col_type_is('public', 'admission_reservations', 'reserved_at', 'timestamp with time zone', 'reserved_at is timestamptz');

-- =================================================================
-- 3. Constraints
-- =================================================================
SELECT has_pk('public', 'admission_reservations', 'admission_reservations has primary key');

SELECT ok(
    (SELECT EXISTS(SELECT 1 FROM pg_constraint
     WHERE conname = 'admission_reservations_pkey'
     AND conrelid = 'admission_reservations'::regclass)),
    'PK constraint name is admission_reservations_pkey'
);

SELECT ok(
    (SELECT EXISTS(SELECT 1 FROM pg_constraint
     WHERE conname = 'admission_reservations_user_id_check'
     AND conrelid = 'admission_reservations'::regclass)),
    'has user_id CHECK constraint'
);

SELECT ok(
    (SELECT EXISTS(SELECT 1 FROM pg_constraint
     WHERE conname = 'admission_reservations_message_hmac_check'
     AND conrelid = 'admission_reservations'::regclass)),
    'has message_hmac CHECK constraint'
);

SELECT ok(
    (SELECT EXISTS(SELECT 1 FROM pg_constraint
     WHERE conname = 'admission_reservations_network_hmac_check'
     AND conrelid = 'admission_reservations'::regclass)),
    'has network_hmac CHECK constraint'
);

SELECT ok(
    (SELECT EXISTS(SELECT 1 FROM pg_constraint
     WHERE conname = 'admission_reservations_estimated_units_check'
     AND conrelid = 'admission_reservations'::regclass)),
    'has estimated_units CHECK constraint'
);

-- Check constraint expression for message_hmac (lowercase hex, exactly 64)
SELECT ok(
    (SELECT pg_get_expr(conbin, conrelid)::text LIKE '%^[0-9a-f]{64}$%'
     FROM pg_constraint WHERE conname = 'admission_reservations_message_hmac_check'),
    'message_hmac CHECK validates lowercase hex 64 chars'
);

SELECT ok(
    (SELECT pg_get_expr(conbin, conrelid)::text LIKE '%^[0-9a-f]{64}$%'
     FROM pg_constraint WHERE conname = 'admission_reservations_network_hmac_check'),
    'network_hmac CHECK validates lowercase hex 64 chars'
);

-- Check estimated_units range
SELECT ok(
    (SELECT pg_get_expr(conbin, conrelid)::text LIKE '%1%6000%'
     FROM pg_constraint WHERE conname = 'admission_reservations_estimated_units_check'),
    'estimated_units CHECK validates range 1-6000'
);

-- =================================================================
-- 4. Indices
-- =================================================================
SELECT has_index('public', 'admission_reservations', 'admission_reservations_user_time_idx',
    'user_time index exists');
SELECT has_index('public', 'admission_reservations', 'admission_reservations_network_time_idx',
    'network_time index exists');
SELECT has_index('public', 'admission_reservations', 'admission_reservations_time_idx',
    'global time index exists');

SELECT index_is_type('public', 'admission_reservations', 'admission_reservations_user_time_idx', 'btree');
SELECT index_is_type('public', 'admission_reservations', 'admission_reservations_network_time_idx', 'btree');
SELECT index_is_type('public', 'admission_reservations', 'admission_reservations_time_idx', 'btree');

-- =================================================================
-- 5. RLS + FORCE RLS
-- =================================================================
SELECT ok(
    (SELECT relrowsecurity FROM pg_class WHERE oid = 'public.admission_reservations'::regclass),
    'RLS is enabled on admission_reservations'
);
SELECT ok(
    (SELECT relforcerowsecurity FROM pg_class WHERE oid = 'public.admission_reservations'::regclass),
    'FORCE RLS is enabled on admission_reservations'
);

-- =================================================================
-- 6. No policies
-- =================================================================
SELECT policies_are('public', 'admission_reservations', ARRAY[]::text[],
    'no policies on admission_reservations');

-- =================================================================
-- 7. Table privileges: no role has any direct access
-- =================================================================
SELECT table_privs_are('public', 'admission_reservations', 'anon', ARRAY[]::text[]);
SELECT table_privs_are('public', 'admission_reservations', 'authenticated', ARRAY[]::text[]);
SELECT table_privs_are('public', 'admission_reservations', 'service_role', ARRAY[]::text[]);

SELECT ok(
    NOT has_table_privilege('public', 'public.admission_reservations',
        'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER'),
    'PUBLIC has no privileges on admission_reservations'
);

-- =================================================================
-- 8. RPC exists with correct signature
-- =================================================================
SELECT ok(
    (SELECT EXISTS(
        SELECT 1 FROM pg_proc p
        JOIN pg_namespace n ON p.pronamespace = n.oid
        WHERE n.nspname = 'public'
        AND p.proname = 'reserve_admission'
        AND pg_get_function_identity_arguments(p.oid) =
            'p_user_id text, p_request_id uuid, p_message_hmac_sha256 text, p_network_hmac_sha256 text, p_estimated_units integer'
    )),
    'reserve_admission RPC exists with correct signature'
);

-- =================================================================
-- 9. RPC is SECURITY DEFINER
-- =================================================================
SELECT ok(
    (SELECT p.prosecdef FROM pg_proc p
     JOIN pg_namespace n ON p.pronamespace = n.oid
     WHERE n.nspname = 'public' AND p.proname = 'reserve_admission'),
    'reserve_admission is SECURITY DEFINER'
);

-- =================================================================
-- 10. RPC search_path is set to public
-- =================================================================
--
-- pg_get_functiondef returns the full CREATE OR REPLACE text which
-- includes the SET search_path clause set by the migration.
SELECT ok(
    pg_get_functiondef(p.oid) LIKE '%SET search_path = public%'
    FROM pg_proc p
    JOIN pg_namespace n ON p.pronamespace = n.oid
    WHERE n.nspname = 'public' AND p.proname = 'reserve_admission',
    'RPC has SET search_path = public'
);

-- =================================================================
-- 11. RPC EXECUTE granted only to service_role
-- =================================================================
SELECT function_privs_are('public', 'reserve_admission',
    ARRAY['text', 'uuid', 'text', 'text', 'integer'],
    'anon', ARRAY[]::text[],
    'anon has no EXECUTE on reserve_admission'
);

SELECT function_privs_are('public', 'reserve_admission',
    ARRAY['text', 'uuid', 'text', 'text', 'integer'],
    'authenticated', ARRAY[]::text[],
    'authenticated has no EXECUTE on reserve_admission'
);

SELECT function_privs_are('public', 'reserve_admission',
    ARRAY['text', 'uuid', 'text', 'text', 'integer'],
    'service_role', ARRAY['EXECUTE'],
    'service_role has EXECUTE on reserve_admission'
);

SELECT ok(
    NOT has_function_privilege('public',
        'public.reserve_admission(text, uuid, text, text, integer)',
        'EXECUTE'),
    'PUBLIC has no EXECUTE on reserve_admission'
);

-- =================================================================
-- 12. RPC returns TABLE(decision text, retry_after_seconds integer)
-- =================================================================
SELECT ok(
    (SELECT EXISTS(
        SELECT 1 FROM pg_proc p
        JOIN pg_namespace n ON p.pronamespace = n.oid
        WHERE n.nspname = 'public'
        AND p.proname = 'reserve_admission'
        AND p.proretset = true  -- RETURNS TABLE / SETOF
    )),
    'reserve_admission returns a set (TABLE)'
);

-- =================================================================
-- 13. RPC has SECURITY DEFINER (already checked above via prosecdef)
--     This is the same as test #9 — skipped here to avoid duplicate.
-- =================================================================

-- =================================================================
-- 14. No dynamic SQL in RPC function body
-- =================================================================
SELECT ok(
    (SELECT pg_get_functiondef(p.oid) NOT LIKE '%EXECUTE%' AND
            pg_get_functiondef(p.oid) NOT LIKE '%format(%' AND
            pg_get_functiondef(p.oid) NOT LIKE '%quote_ident%' AND
            pg_get_functiondef(p.oid) NOT LIKE '%quote_literal%'
     FROM pg_proc p
     JOIN pg_namespace n ON p.pronamespace = n.oid
     WHERE n.nspname = 'public' AND p.proname = 'reserve_admission'),
    'reserve_admission contains no dynamic SQL'
);

SELECT * FROM finish();
ROLLBACK;
