-- 20260807201256_harden_rls_auto_enable.sql
-- Version the security decision for the legacy hosted function
-- public.rls_auto_enable() (#291).
--
-- Decision: PRESERVE_AND_HARDEN
--   * The function and its event trigger are legacy hosted objects that are
--     NOT versioned by this repository. A hosted audit found the function
--     exposed as SECURITY DEFINER with EXECUTE granted to PUBLIC, anon,
--     authenticated and service_role, which is a privilege drift for a
--     privileged function.
--   * This migration does NOT remove the function or its event trigger (that
--     decision needs separate evidence and a separate issue). It only revokes
--     the runtime EXECUTE grants, keeping the owner able to administer the
--     object under normal PostgreSQL semantics.
--
-- Behavior:
--   * Clean database (object absent): the catalog gate does not match, the
--     REVOKE is skipped, nothing is created and the migration is a no-op.
--   * Legacy upgrade (object present): the object is identified EXACTLY by
--     schema (public), name (rls_auto_enable), zero arguments and return
--     type event_trigger; EXECUTE is revoked from PUBLIC, anon,
--     authenticated and service_role only. The function body, owner,
--     search_path and the associated event trigger are left untouched.
--   * Idempotent: re-evaluating the block when the privileges are already
--     removed succeeds without recreating grants or altering the object.
--
-- No dynamic SQL is used at runtime; the REVOKE is a static statement with
-- constant, verified identifiers, executed only when the exact object is
-- confirmed in the catalogs.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
          AND p.proname = 'rls_auto_enable'
          AND p.pronargs = 0
          AND p.prorettype = 'event_trigger'::regtype
    ) THEN
        REVOKE EXECUTE ON FUNCTION public.rls_auto_enable()
            FROM PUBLIC, anon, authenticated, service_role;
    END IF;
END $$;
