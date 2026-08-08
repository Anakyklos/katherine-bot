-- Legacy drift fixture for #291 (PRESERVE + VERSION + HARDEN).
--
-- Reproduces the hosted state confirmed by the post-hardening inspection of
-- the Supabase project BEFORE the canonical migration converges it:
--
--   * public.rls_auto_enable(): zero arguments, returns event_trigger,
--     plpgsql, SECURITY DEFINER, owner postgres, search_path pg_catalog.
--   * Event trigger "ensure_rls" on ddl_command_end, enabled, tags
--     CREATE TABLE / CREATE TABLE AS / SELECT INTO, bound to the function.
--   * EXECUTE granted to PUBLIC, anon, authenticated and service_role.
--   * A legacy body that tries to auto-enable RLS but builds identifiers
--     from a single unquoted text string and swallows every failure with
--     WHEN OTHERS into a generic log (the problematic structure the
--     canonical migration must converge away from).
--
-- The fixture is test-only: it never depends on the hosted project. Its
-- purpose is to prove that the migration replaces the unreviewed body with
-- the canonical definition and revokes the broad grants, not to preserve
-- the legacy body byte for byte.

CREATE OR REPLACE FUNCTION public.rls_auto_enable() RETURNS event_trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    v_table text;
BEGIN
    SELECT quote_ident(n.nspname) || '.' || quote_ident(c.relname)
    INTO v_table
    FROM pg_catalog.pg_event_trigger_ddl_commands() d
    JOIN pg_catalog.pg_class c ON c.oid = d.objid
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    LIMIT 1;

    IF v_table IS NULL THEN
        RETURN;
    END IF;

    BEGIN
        EXECUTE 'ALTER TABLE ' || v_table || ' ENABLE ROW LEVEL SECURITY';
    EXCEPTION WHEN OTHERS THEN
        RAISE LOG 'rls_auto_enable: could not enable RLS on %', v_table;
    END;
END;
$$;

CREATE EVENT TRIGGER ensure_rls
ON ddl_command_end
WHEN TAG IN ('CREATE TABLE', 'CREATE TABLE AS', 'SELECT INTO')
EXECUTE FUNCTION public.rls_auto_enable();

GRANT EXECUTE ON FUNCTION public.rls_auto_enable()
    TO PUBLIC, anon, authenticated, service_role;
