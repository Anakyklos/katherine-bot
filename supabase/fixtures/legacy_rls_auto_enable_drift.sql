-- Legacy drift fixture for #291 (PRESERVE_AND_HARDEN).
--
-- Reproduces the hosted state observed by the security audit BEFORE the
-- hardening migration: public.rls_auto_enable() exposed as SECURITY DEFINER
-- with EXECUTE granted to PUBLIC, anon, authenticated and service_role, plus
-- an event trigger associated with the function.
--
-- The fixture is intentionally SAFE for tests:
--   * no-op body: it does NOT enable RLS on new tables and does not write data
--   * preserves only the exposed surface observed in the audit:
--       - zero arguments
--       - returns event_trigger
--       - SECURITY DEFINER
--       - owner = postgres (the test environment migration owner)
--       - search_path = pg_catalog
--   * one event trigger referencing the function

CREATE OR REPLACE FUNCTION public.rls_auto_enable() RETURNS event_trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    NULL;
END;
$$;

CREATE EVENT TRIGGER rls_auto_enable_legacy_trigger
ON ddl_command_end
WHEN TAG IN ('CREATE TABLE')
EXECUTE FUNCTION public.rls_auto_enable();

GRANT EXECUTE ON FUNCTION public.rls_auto_enable()
    TO PUBLIC, anon, authenticated, service_role;
