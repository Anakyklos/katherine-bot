-- 20260807201256_harden_rls_auto_enable.sql
-- Version the canonical rls_auto_enable() mechanism (#291).
--
-- Decision: PRESERVE + VERSION + HARDEN
--
-- The hosted Supabase project was inspected after the initial hardening and
-- confirmed to run a real, active mechanism:
--   * public.rls_auto_enable(): zero arguments, returns event_trigger,
--     plpgsql, SECURITY DEFINER, owner postgres, search_path pg_catalog.
--   * Event trigger "ensure_rls" on ddl_command_end, enabled, tags
--     CREATE TABLE / CREATE TABLE AS / SELECT INTO.
--   * The mechanism automatically enables ROW LEVEL SECURITY on new tables
--     in the public schema.
--   * ACL drift: EXECUTE was granted to PUBLIC, anon, authenticated and
--     service_role, and the legacy body used dynamic SQL plus a
--     WHEN OTHERS handler that swallowed failures into a generic log.
--
-- The exact origin of the hosted object was not found in the versioned
-- history of this repository, so it is treated as legacy drift of unproven
-- origin. Instead of preserving the unversioned, unreviewed body, this
-- migration brings the mechanism INTO the versioned schema (VERSION),
-- replaces the legacy body with a reviewed canonical definition (PRESERVE +
-- HARDEN: the mechanism itself is kept and hardened), and revokes the broad
-- runtime EXECUTE grants.
--
-- Behavior:
--   * Clean database (object absent): creates the canonical function and the
--     canonical "ensure_rls" event trigger, then revokes EXECUTE from
--     PUBLIC, anon, authenticated and service_role.
--   * Legacy database (object present): replaces the legacy body with the
--     canonical definition (CREATE OR REPLACE keeps the OID, owner and
--     ACL), reconciles the "ensure_rls" event trigger to the canonical
--     configuration, and revokes the runtime EXECUTE grants. Only the
--     explicitly known drift is converged: an unrecognized event trigger
--     pointing at the function, an unexpected owner, or any remaining
--     EXECUTE grantee other than postgres makes the migration fail
--     explicitly instead of being normalized or destroyed silently.
--   * Idempotent: re-evaluating the block converges to the same canonical
--     state without duplicating objects or recreating grants.
--
-- The canonical body is fail-closed: if the mechanism should protect a new
-- public table and enabling RLS fails, the error propagates and the DDL
-- command fails instead of silently leaving an unprotected table. No user-
-- controlled text is used to form identifiers; the created relation is
-- resolved by OID from the catalogs and any ALTER TABLE is built with
-- properly escaped identifiers (pg_catalog.format %I).

CREATE OR REPLACE FUNCTION public.rls_auto_enable()
RETURNS event_trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_schema name;
    v_relname name;
    v_relkind "char";
    v_rls boolean;
    v_cmd record;
BEGIN
    FOR v_cmd IN
        SELECT d.objid
        FROM pg_catalog.pg_event_trigger_ddl_commands() d
        WHERE d.classid = 'pg_catalog.pg_class'::pg_catalog.regclass
    LOOP
        SELECT n.nspname, c.relname, c.relkind, c.relrowsecurity
        INTO v_schema, v_relname, v_relkind, v_rls
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE c.oid = v_cmd.objid;

        IF NOT FOUND THEN
            RAISE EXCEPTION
                'rls_auto_enable: relation % not found in the catalogs',
                v_cmd.objid;
        END IF;

        -- Only relations in the public schema are covered by the mechanism.
        IF v_schema <> 'public' THEN
            CONTINUE;
        END IF;

        -- Only plain and partitioned tables are covered (the trigger fires
        -- for CREATE TABLE, CREATE TABLE AS and SELECT INTO).
        IF v_relkind NOT IN ('r', 'p') THEN
            CONTINUE;
        END IF;

        IF v_rls THEN
            CONTINUE;
        END IF;

        EXECUTE pg_catalog.format(
            'ALTER TABLE %I.%I ENABLE ROW LEVEL SECURITY',
            v_schema,
            v_relname
        );
    END LOOP;
END
$function$;

DO $$
DECLARE
    v_fn_oid oid;
    v_dup record;
    v_trg record;
    v_canonical_tags text[] := ARRAY[
        'CREATE TABLE', 'CREATE TABLE AS', 'SELECT INTO'
    ];
BEGIN
    SELECT p.oid INTO v_fn_oid
    FROM pg_catalog.pg_proc p
    JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public'
      AND p.proname = 'rls_auto_enable'
      AND p.pronargs = 0
      AND p.prorettype = 'event_trigger'::pg_catalog.regtype;

    IF v_fn_oid IS NULL THEN
        RAISE EXCEPTION
            'rls_auto_enable: canonical function missing after creation';
    END IF;

    -- Drift guard: only the canonical ensure_rls trigger is recognized.
    -- An unrecognized event trigger pointing at the function is unknown
    -- drift: the migration must fail explicitly and never destroy it.
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_event_trigger et
        WHERE et.evtfoid = v_fn_oid
          AND et.evtname <> 'ensure_rls'
    ) THEN
        RAISE EXCEPTION
            'rls_auto_enable: unexpected event trigger references the function';
    END IF;

    SELECT *
    INTO v_trg
    FROM pg_catalog.pg_event_trigger
    WHERE evtname = 'ensure_rls';

    IF v_trg.evtname IS NULL THEN
        EXECUTE
            'CREATE EVENT TRIGGER ensure_rls ON ddl_command_end '
            'WHEN TAG IN (''CREATE TABLE'', ''CREATE TABLE AS'', ''SELECT INTO'') '
            'EXECUTE FUNCTION public.rls_auto_enable()';
    ELSE
        IF v_trg.evtfoid <> v_fn_oid THEN
            RAISE EXCEPTION
                'rls_auto_enable: event trigger ensure_rls points to an unexpected function';
        END IF;
        IF v_trg.evtevent <> 'ddl_command_end' THEN
            RAISE EXCEPTION
                'rls_auto_enable: event trigger ensure_rls has unexpected event %',
                v_trg.evtevent;
        END IF;
        IF NOT (
            v_trg.evttags @> v_canonical_tags
            AND v_canonical_tags @> v_trg.evttags
        ) THEN
            RAISE EXCEPTION
                'rls_auto_enable: event trigger ensure_rls has unexpected tags %',
                v_trg.evttags;
        END IF;
        IF v_trg.evtenabled <> 'O' THEN
            EXECUTE 'ALTER EVENT TRIGGER ensure_rls ENABLE';
        END IF;
    END IF;
END $$;

REVOKE EXECUTE ON FUNCTION public.rls_auto_enable()
    FROM PUBLIC, anon, authenticated, service_role;

-- Canonical postcondition: only the known drift is converged. If the final
-- state still carries unknown drift (unexpected owner, or any grantee other
-- than the postgres owner with effective EXECUTE), the migration fails
-- explicitly instead of silently accepting or normalizing it.
DO $$
DECLARE
    v_owner oid;
BEGIN
    SELECT p.proowner INTO v_owner
    FROM pg_catalog.pg_proc p
    JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public'
      AND p.proname = 'rls_auto_enable'
      AND p.pronargs = 0;

    IF v_owner <> 'postgres'::pg_catalog.regrole THEN
        RAISE EXCEPTION 'rls_auto_enable: unexpected function owner';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_proc p
        JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
        CROSS JOIN LATERAL pg_catalog.aclexplode(p.proacl) acl
        WHERE n.nspname = 'public'
          AND p.proname = 'rls_auto_enable'
          AND p.pronargs = 0
          AND acl.privilege_type = 'EXECUTE'
          AND acl.grantee <> p.proowner
    ) THEN
        RAISE EXCEPTION 'rls_auto_enable: unexpected EXECUTE grants remain';
    END IF;
END $$;
