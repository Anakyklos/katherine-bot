BEGIN;

CREATE EXTENSION IF NOT EXISTS pgtap;
SELECT plan(62);

-- =================================================================
-- 1. Canonical mechanism is versioned and clean database converges (#291)
--    After `supabase db reset`, public.rls_auto_enable() MUST exist as the
--    canonical versioned definition and ensure_rls MUST be active. This is
--    the PRESERVE + VERSION + HARDEN contract: clean reset and legacy
--    upgrade converge to the same state.
-- =================================================================

-- The migration is matched by its registered name (derived by the CLI from
-- the migration file name, without the fixed-width version prefix), so
-- renaming the timestamp never leaves this test with a stale hard-coded
-- version.
SELECT ok(
    EXISTS(
        SELECT 1
        FROM supabase_migrations.schema_migrations
        WHERE name = 'harden_rls_auto_enable'
    ),
    'harden_rls_auto_enable migration is registered'
);

-- ---- Canonical function identity (catalog proof) ----
SELECT ok(
    EXISTS(
        SELECT 1
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
          AND p.proname = 'rls_auto_enable'
          AND p.pronargs = 0
    ),
    'public.rls_auto_enable() exists in a clean database'
);

SELECT ok(
    (SELECT n.nspname = 'public'
     FROM pg_proc p
     JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public'
       AND p.proname = 'rls_auto_enable'
       AND p.pronargs = 0),
    'rls_auto_enable is in schema public'
);

SELECT ok(
    (SELECT p.pronargs = 0
     FROM pg_proc p
     JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public'
       AND p.proname = 'rls_auto_enable'
       AND p.pronargs = 0),
    'rls_auto_enable has zero arguments'
);

SELECT ok(
    (SELECT p.prorettype = 'event_trigger'::pg_catalog.regtype
     FROM pg_proc p
     JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public'
       AND p.proname = 'rls_auto_enable'
       AND p.pronargs = 0),
    'rls_auto_enable returns event_trigger'
);

SELECT ok(
    (SELECT p.prosecdef
     FROM pg_proc p
     JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public'
       AND p.proname = 'rls_auto_enable'
       AND p.pronargs = 0),
    'rls_auto_enable is SECURITY DEFINER'
);

SELECT ok(
    (SELECT p.proowner = 'postgres'::pg_catalog.regrole
     FROM pg_proc p
     JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public'
       AND p.proname = 'rls_auto_enable'
       AND p.pronargs = 0),
    'rls_auto_enable is owned by postgres'
);

SELECT ok(
    (SELECT p.proconfig = ARRAY['search_path=pg_catalog']::pg_catalog.text[]
     FROM pg_proc p
     JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public'
       AND p.proname = 'rls_auto_enable'
       AND p.pronargs = 0),
    'rls_auto_enable search_path is exactly pg_catalog'
);

-- ---- ACL: no runtime role or PUBLIC has EXECUTE ----
SELECT ok(
    NOT has_function_privilege(
        'public', 'public.rls_auto_enable()', 'EXECUTE'
    ),
    'PUBLIC has no EXECUTE on rls_auto_enable'
);
SELECT ok(
    NOT has_function_privilege(
        'anon', 'public.rls_auto_enable()', 'EXECUTE'
    ),
    'anon has no EXECUTE on rls_auto_enable'
);
SELECT ok(
    NOT has_function_privilege(
        'authenticated', 'public.rls_auto_enable()', 'EXECUTE'
    ),
    'authenticated has no EXECUTE on rls_auto_enable'
);
SELECT ok(
    NOT has_function_privilege(
        'service_role', 'public.rls_auto_enable()', 'EXECUTE'
    ),
    'service_role has no EXECUTE on rls_auto_enable'
);
SELECT ok(
    has_function_privilege(
        'postgres', 'public.rls_auto_enable()', 'EXECUTE'
    ),
    'owner postgres keeps EXECUTE on rls_auto_enable'
);

-- ---- Canonical event trigger ensure_rls ----
SELECT ok(
    EXISTS(
        SELECT 1 FROM pg_event_trigger WHERE evtname = 'ensure_rls'
    ),
    'event trigger ensure_rls exists'
);

SELECT ok(
    EXISTS(
        SELECT 1
        FROM pg_event_trigger et
        JOIN pg_proc p ON p.oid = et.evtfoid
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE et.evtname = 'ensure_rls'
          AND n.nspname = 'public'
          AND p.proname = 'rls_auto_enable'
          AND p.pronargs = 0
    ),
    'ensure_rls is bound to public.rls_auto_enable()'
);

SELECT ok(
    (SELECT evtevent = 'ddl_command_end'
     FROM pg_event_trigger WHERE evtname = 'ensure_rls'),
    'ensure_rls fires on ddl_command_end'
);

SELECT ok(
    (SELECT evtenabled = 'O'
     FROM pg_event_trigger WHERE evtname = 'ensure_rls'),
    'ensure_rls is enabled'
);

SELECT ok(
    (SELECT evttags @> ARRAY[
         'CREATE TABLE', 'CREATE TABLE AS', 'SELECT INTO'
       ]::pg_catalog.text[]
       AND ARRAY[
         'CREATE TABLE', 'CREATE TABLE AS', 'SELECT INTO'
       ]::pg_catalog.text[] @> evttags
     FROM pg_event_trigger WHERE evtname = 'ensure_rls'),
    'ensure_rls has exactly the canonical tags'
);

SELECT ok(
    (SELECT count(*) = 1
     FROM pg_event_trigger et
     JOIN pg_proc p ON p.oid = et.evtfoid
     JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public'
       AND p.proname = 'rls_auto_enable'
       AND p.pronargs = 0),
    'exactly one event trigger points at rls_auto_enable'
);

-- ---- Behavioral proof: new public tables get RLS automatically ----
CREATE TABLE public.pgtap_rls_probe (id int);
SELECT ok(
    (SELECT relrowsecurity FROM pg_catalog.pg_class
     WHERE oid = 'public.pgtap_rls_probe'::pg_catalog.regclass),
    'ensure_rls auto-enables RLS on a new public table'
);
DROP TABLE public.pgtap_rls_probe;
SELECT ok(
    NOT EXISTS(
        SELECT 1 FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relname = 'pgtap_rls_probe'
    ),
    'probe table is removed after the behavioral test'
);

-- =================================================================
-- 2. RLS and FORCE RLS remain intact on every server-owned table
-- =================================================================

SELECT ok(
    (SELECT relrowsecurity FROM pg_class WHERE oid = 'public.profiles'::regclass),
    'RLS is enabled on profiles'
);
SELECT ok(
    (SELECT relforcerowsecurity FROM pg_class WHERE oid = 'public.profiles'::regclass),
    'FORCE RLS is enabled on profiles'
);
SELECT ok(
    (SELECT relrowsecurity FROM pg_class WHERE oid = 'public.chat_logs'::regclass),
    'RLS is enabled on chat_logs'
);
SELECT ok(
    (SELECT relforcerowsecurity FROM pg_class WHERE oid = 'public.chat_logs'::regclass),
    'FORCE RLS is enabled on chat_logs'
);
SELECT ok(
    (SELECT relrowsecurity FROM pg_class WHERE oid = 'public.memories'::regclass),
    'RLS is enabled on memories'
);
SELECT ok(
    (SELECT relforcerowsecurity FROM pg_class WHERE oid = 'public.memories'::regclass),
    'FORCE RLS is enabled on memories'
);
SELECT ok(
    (SELECT relrowsecurity FROM pg_class WHERE oid = 'public.archival_extractions'::regclass),
    'RLS is enabled on archival_extractions'
);
SELECT ok(
    (SELECT relforcerowsecurity FROM pg_class WHERE oid = 'public.archival_extractions'::regclass),
    'FORCE RLS is enabled on archival_extractions'
);
SELECT ok(
    (SELECT relrowsecurity FROM pg_class WHERE oid = 'public.turn_requests'::regclass),
    'RLS is enabled on turn_requests'
);
SELECT ok(
    (SELECT relforcerowsecurity FROM pg_class WHERE oid = 'public.turn_requests'::regclass),
    'FORCE RLS is enabled on turn_requests'
);
SELECT ok(
    (SELECT relrowsecurity FROM pg_class WHERE oid = 'public.outbox_events'::regclass),
    'RLS is enabled on outbox_events'
);
SELECT ok(
    (SELECT relforcerowsecurity FROM pg_class WHERE oid = 'public.outbox_events'::regclass),
    'FORCE RLS is enabled on outbox_events'
);

-- =================================================================
-- 3. Legitimate project RPC grants are unchanged
--    Every versioned SECURITY DEFINER RPC keeps its exact ACL:
--    EXECUTE for service_role only; no EXECUTE for anon, authenticated
--    or PUBLIC.
-- =================================================================

SELECT ok(
    has_function_privilege(
        'service_role', 'public.match_memories(vector, double precision, integer, text)', 'EXECUTE'
    ),
    'service_role has EXECUTE on match_memories'
);
SELECT ok(
    NOT has_function_privilege(
        'anon', 'public.match_memories(vector, double precision, integer, text)', 'EXECUTE'
    ),
    'anon has no EXECUTE on match_memories'
);
SELECT ok(
    NOT has_function_privilege(
        'authenticated', 'public.match_memories(vector, double precision, integer, text)', 'EXECUTE'
    ),
    'authenticated has no EXECUTE on match_memories'
);
SELECT ok(
    NOT has_function_privilege(
        'public', 'public.match_memories(vector, double precision, integer, text)', 'EXECUTE'
    ),
    'PUBLIC has no EXECUTE on match_memories'
);

SELECT ok(
    has_function_privilege(
        'service_role', 'public.reserve_admission(text, uuid, text, text, integer)', 'EXECUTE'
    ),
    'service_role has EXECUTE on reserve_admission'
);
SELECT ok(
    NOT has_function_privilege(
        'anon', 'public.reserve_admission(text, uuid, text, text, integer)', 'EXECUTE'
    ),
    'anon has no EXECUTE on reserve_admission'
);
SELECT ok(
    NOT has_function_privilege(
        'authenticated', 'public.reserve_admission(text, uuid, text, text, integer)', 'EXECUTE'
    ),
    'authenticated has no EXECUTE on reserve_admission'
);
SELECT ok(
    NOT has_function_privilege(
        'public', 'public.reserve_admission(text, uuid, text, text, integer)', 'EXECUTE'
    ),
    'PUBLIC has no EXECUTE on reserve_admission'
);

SELECT ok(
    has_function_privilege(
        'service_role', 'public.commit_turn_build_result(text, uuid)', 'EXECUTE'
    ),
    'service_role has EXECUTE on commit_turn_build_result'
);
SELECT ok(
    NOT has_function_privilege(
        'anon', 'public.commit_turn_build_result(text, uuid)', 'EXECUTE'
    ),
    'anon has no EXECUTE on commit_turn_build_result'
);
SELECT ok(
    NOT has_function_privilege(
        'authenticated', 'public.commit_turn_build_result(text, uuid)', 'EXECUTE'
    ),
    'authenticated has no EXECUTE on commit_turn_build_result'
);
SELECT ok(
    NOT has_function_privilege(
        'public', 'public.commit_turn_build_result(text, uuid)', 'EXECUTE'
    ),
    'PUBLIC has no EXECUTE on commit_turn_build_result'
);

SELECT ok(
    has_function_privilege(
        'service_role',
        'public.commit_turn(text, uuid, bigint, text, text, text, jsonb, jsonb, text, jsonb, jsonb, text)',
        'EXECUTE'
    ),
    'service_role has EXECUTE on commit_turn'
);
SELECT ok(
    NOT has_function_privilege(
        'anon',
        'public.commit_turn(text, uuid, bigint, text, text, text, jsonb, jsonb, text, jsonb, jsonb, text)',
        'EXECUTE'
    ),
    'anon has no EXECUTE on commit_turn'
);
SELECT ok(
    NOT has_function_privilege(
        'authenticated',
        'public.commit_turn(text, uuid, bigint, text, text, text, jsonb, jsonb, text, jsonb, jsonb, text)',
        'EXECUTE'
    ),
    'authenticated has no EXECUTE on commit_turn'
);
SELECT ok(
    NOT has_function_privilege(
        'public',
        'public.commit_turn(text, uuid, bigint, text, text, text, jsonb, jsonb, text, jsonb, jsonb, text)',
        'EXECUTE'
    ),
    'PUBLIC has no EXECUTE on commit_turn'
);

SELECT ok(
    has_function_privilege(
        'service_role', 'public.replay_committed_turn(text, uuid)', 'EXECUTE'
    ),
    'service_role has EXECUTE on replay_committed_turn'
);
SELECT ok(
    NOT has_function_privilege(
        'anon', 'public.replay_committed_turn(text, uuid)', 'EXECUTE'
    ),
    'anon has no EXECUTE on replay_committed_turn'
);
SELECT ok(
    NOT has_function_privilege(
        'authenticated', 'public.replay_committed_turn(text, uuid)', 'EXECUTE'
    ),
    'authenticated has no EXECUTE on replay_committed_turn'
);
SELECT ok(
    NOT has_function_privilege(
        'public', 'public.replay_committed_turn(text, uuid)', 'EXECUTE'
    ),
    'PUBLIC has no EXECUTE on replay_committed_turn'
);

SELECT ok(
    has_function_privilege(
        'service_role', 'public.jsonb_snapshot_contract(jsonb, text)', 'EXECUTE'
    ),
    'service_role has EXECUTE on jsonb_snapshot_contract'
);
SELECT ok(
    NOT has_function_privilege(
        'anon', 'public.jsonb_snapshot_contract(jsonb, text)', 'EXECUTE'
    ),
    'anon has no EXECUTE on jsonb_snapshot_contract'
);
SELECT ok(
    NOT has_function_privilege(
        'authenticated', 'public.jsonb_snapshot_contract(jsonb, text)', 'EXECUTE'
    ),
    'authenticated has no EXECUTE on jsonb_snapshot_contract'
);
SELECT ok(
    NOT has_function_privilege(
        'public', 'public.jsonb_snapshot_contract(jsonb, text)', 'EXECUTE'
    ),
    'PUBLIC has no EXECUTE on jsonb_snapshot_contract'
);

SELECT ok(
    has_function_privilege(
        'service_role', 'public.turn_requests_null_message_refs()', 'EXECUTE'
    ),
    'service_role has EXECUTE on turn_requests_null_message_refs'
);
SELECT ok(
    NOT has_function_privilege(
        'anon', 'public.turn_requests_null_message_refs()', 'EXECUTE'
    ),
    'anon has no EXECUTE on turn_requests_null_message_refs'
);
SELECT ok(
    NOT has_function_privilege(
        'authenticated', 'public.turn_requests_null_message_refs()', 'EXECUTE'
    ),
    'authenticated has no EXECUTE on turn_requests_null_message_refs'
);
SELECT ok(
    NOT has_function_privilege(
        'public', 'public.turn_requests_null_message_refs()', 'EXECUTE'
    ),
    'PUBLIC has no EXECUTE on turn_requests_null_message_refs'
);

-- =================================================================
-- 4. No new EXECUTE access was granted to runtime roles or PUBLIC
-- =================================================================

SELECT ok(
    NOT EXISTS(
        SELECT 1
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
          AND p.proname IN (
              'match_memories', 'reserve_admission', 'commit_turn',
              'commit_turn_build_result', 'replay_committed_turn',
              'jsonb_snapshot_contract', 'turn_requests_null_message_refs',
              'rls_auto_enable'
          )
          AND (
              has_function_privilege('anon', p.oid, 'EXECUTE')
              OR has_function_privilege('authenticated', p.oid, 'EXECUTE')
              OR has_function_privilege('public', p.oid, 'EXECUTE')
          )
    ),
    'no runtime role or PUBLIC gained EXECUTE on project functions'
);

SELECT * FROM finish();
ROLLBACK;
