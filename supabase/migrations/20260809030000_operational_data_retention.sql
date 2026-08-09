-- 20260809030000_operational_data_retention.sql
-- Operational data retention policy (#316).
--
-- Scope
-- =====
-- This migration adds the server-side purge boundary for OPERATIONAL data
-- only. It deliberately never touches user-controlled content:
--
--   * admission_reservations   — rows with reserved_at OLDER than the purge
--     cutoff (the runner computes now - 24h) are eligible. Rows inside the
--     horizon stay: the anti-abuse ledger is preserved and delete_history
--     continues to never touch it, so quota cannot be bypassed via cleanup.
--   * privacy_operations       — applied ledger rows with applied_at OLDER
--     than the purge cutoff (the runner computes now - 30d) are eligible.
--     The idempotency/conflict semantics of #314 are unchanged: the ledger
--     row is only removed after its horizon, and replay/conflict behavior
--     inside the horizon is fully preserved.
--   * outbox_events            — ONLY final states (completed, dead_letter)
--     whose retention_until is PAST the purge cutoff (the runner passes
--     now) are eligible. Active states (pending, processing, failed) are
--     never purged by age.
--
-- DB-authoritative cutoff clamp (security invariant)
-- ==================================================
-- The three purge RPCs accept a caller-supplied p_cutoff but NEVER trust it
-- absolutely: each function clamps the effective cutoff against
-- authoritative PostgreSQL time (clock_timestamp()) so that the binding
-- minimum retention horizons are enforced BY THE DATABASE:
--
--   * admission_reservations: effective cutoff <= db_now - 24h;
--   * privacy_operations:     effective cutoff <= db_now - 30 days;
--   * outbox_events:          effective cutoff <= db_now.
--
-- A caller cutoff (or a fast/misconfigured process clock) can therefore
-- only REDUCE progress (an older cutoff purges fewer rows); it can never
-- ADVANCE deletion: rows inside the horizons, and outbox events whose
-- retention_until is still in the future, are protected regardless of the
-- caller. The runner's process clock is advisory for scheduling only.
--
-- NOT covered: chat_logs, memories, archival_extractions, profiles
-- snapshots, turn_requests. User-controlled data and the replay/history
-- ledger get NO automatic TTL; they remain until an explicit user action or
-- future account deletion.
--
-- Fail-closed preflight
-- =====================
-- The migration refuses to install when ANY mandatory dependency is missing
-- (SQLSTATE 23514), mirroring the privacy_data_operations pattern: a schema
-- that lost one of the three operational tables must never receive the
-- purge functions.
--
-- Concurrency
-- ===========
-- Purge is safe under concurrent executions WITHOUT any global advisory
-- lock: every statement is a bounded, idempotent DELETE whose WHERE set is
-- selected by primary key inside a LIMIT-bounded subquery. Two concurrent
-- runs may select overlapping batches; each row is deleted exactly once
-- (the loser's DELETE simply finds the row gone) and no user/writer is ever
-- blocked for more than a single row-level delete. No LOCK TABLE, no
-- long-lived locks.
--
-- Grants
-- ======
-- The three functions are SECURITY DEFINER (owner postgres) with
-- SET search_path = '' and EXECUTE granted to service_role ONLY. PUBLIC,
-- anon and authenticated have no access. The RLS/FORCE-RLS server-owned
-- tables are only ever reachable through these functions.

-- =================================================================
-- 0. Preflight (fail closed on any missing dependency)
-- =================================================================
DO $$
DECLARE
    v_missing text;
BEGIN
    IF pg_catalog.to_regclass('public.admission_reservations') IS NULL THEN
        v_missing := 'public.admission_reservations';
    ELSIF pg_catalog.to_regclass('public.privacy_operations') IS NULL THEN
        v_missing := 'public.privacy_operations';
    ELSIF pg_catalog.to_regclass('public.outbox_events') IS NULL THEN
        v_missing := 'public.outbox_events';
    END IF;

    IF v_missing IS NOT NULL THEN
        RAISE EXCEPTION 'missing mandatory dependency: %', v_missing
            USING ERRCODE = '23514';
    END IF;
END;
$$;

-- =================================================================
-- 1. Indexes for bounded purge scans
-- =================================================================
CREATE INDEX IF NOT EXISTS privacy_operations_applied_at_idx
    ON public.privacy_operations (applied_at);

CREATE INDEX IF NOT EXISTS outbox_events_status_retention_until_idx
    ON public.outbox_events (status, retention_until);

-- admission_reservations already has admission_reservations_time_idx
-- (reserved_at DESC), which the purge predicate uses directly.

-- =================================================================
-- 2. Shared fail-closed validation helper
-- =================================================================
-- Returns an error constant or NULL. Never echoes the supplied values: the
-- caller raises the constant sanitized message.
CREATE OR REPLACE FUNCTION public.retention_purge_validation_error(
    p_cutoff timestamptz,
    p_batch_size integer
) RETURNS text
LANGUAGE plpgsql
IMMUTABLE
SET search_path = ''
AS $$
BEGIN
    IF p_cutoff IS NULL THEN
        RETURN 'retention purge requires a cutoff';
    END IF;
    IF p_batch_size IS NULL OR p_batch_size < 1 OR p_batch_size > 1000 THEN
        RETURN 'retention purge batch_size must be between 1 and 1000';
    END IF;
    RETURN NULL;
END;
$$;

REVOKE ALL ON FUNCTION public.retention_purge_validation_error(timestamptz, integer)
    FROM PUBLIC, anon, authenticated, service_role;

-- =================================================================
-- 3. purge_admission_reservations
-- =================================================================
-- Deletes up to p_batch_size rows with reserved_at < effective cutoff and
-- returns the number of rows deleted. Rows at or after the cutoff (current
-- quota ledger) are never touched.
--
-- The effective cutoff is the caller-supplied p_cutoff CLAMPED to never
-- exceed db_now - 24h, using authoritative PostgreSQL time
-- (clock_timestamp()). The database therefore enforces the binding 24h
-- minimum retention horizon: a caller cutoff (or a fast process clock)
-- can only reduce progress, never advance deletion; rows inside the
-- horizon are protected regardless of the caller.
CREATE OR REPLACE FUNCTION public.purge_admission_reservations(
    p_cutoff timestamptz,
    p_batch_size integer
) RETURNS integer
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_cutoff timestamptz;
    v_deleted integer;
BEGIN
    IF public.retention_purge_validation_error(p_cutoff, p_batch_size) IS NOT NULL THEN
        RAISE EXCEPTION 'invalid retention parameters';
    END IF;

    v_cutoff := LEAST(p_cutoff, clock_timestamp() - interval '24 hours');

    DELETE FROM public.admission_reservations
    WHERE (user_id, request_id) IN (
        SELECT user_id, request_id
        FROM public.admission_reservations
        WHERE reserved_at < v_cutoff
        ORDER BY reserved_at
        LIMIT p_batch_size
    );
    GET DIAGNOSTICS v_deleted = ROW_COUNT;
    RETURN v_deleted;
END;
$$;

REVOKE ALL ON FUNCTION public.purge_admission_reservations(timestamptz, integer)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.purge_admission_reservations(timestamptz, integer)
    TO service_role;

-- =================================================================
-- 4. purge_privacy_operations
-- =================================================================
-- Deletes up to p_batch_size ledger rows with applied_at < effective
-- cutoff and returns the number of rows deleted. Rows inside the retention
-- horizon stay, preserving the #314 replay/idempotency/conflict semantics.
--
-- The effective cutoff is the caller-supplied p_cutoff CLAMPED to never
-- exceed db_now - 30 days (authoritative PostgreSQL time). The database
-- enforces the binding 30-day minimum retention horizon regardless of the
-- caller's cutoff or clock.
CREATE OR REPLACE FUNCTION public.purge_privacy_operations(
    p_cutoff timestamptz,
    p_batch_size integer
) RETURNS integer
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_cutoff timestamptz;
    v_deleted integer;
BEGIN
    IF public.retention_purge_validation_error(p_cutoff, p_batch_size) IS NOT NULL THEN
        RAISE EXCEPTION 'invalid retention parameters';
    END IF;

    v_cutoff := LEAST(p_cutoff, clock_timestamp() - interval '30 days');

    DELETE FROM public.privacy_operations
    WHERE (user_id, operation_id) IN (
        SELECT user_id, operation_id
        FROM public.privacy_operations
        WHERE applied_at < v_cutoff
        ORDER BY applied_at
        LIMIT p_batch_size
    );
    GET DIAGNOSTICS v_deleted = ROW_COUNT;
    RETURN v_deleted;
END;
$$;

REVOKE ALL ON FUNCTION public.purge_privacy_operations(timestamptz, integer)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.purge_privacy_operations(timestamptz, integer)
    TO service_role;

-- =================================================================
-- 5. purge_outbox_events
-- =================================================================
-- Deletes up to p_batch_size FINAL outbox events whose retention_until is
-- PAST the effective cutoff and returns the number of rows deleted. The
-- status predicate is explicit and fail-closed: pending, processing and
-- failed events are never purged by age, and a final event whose
-- retention_until has not yet expired stays.
--
-- The effective cutoff is the caller-supplied p_cutoff CLAMPED to never
-- exceed db_now (authoritative PostgreSQL time). The database therefore
-- enforces the binding rule that an event is only removable after its
-- retention_until has passed, regardless of the caller's cutoff or clock.
CREATE OR REPLACE FUNCTION public.purge_outbox_events(
    p_cutoff timestamptz,
    p_batch_size integer
) RETURNS integer
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_cutoff timestamptz;
    v_deleted integer;
BEGIN
    IF public.retention_purge_validation_error(p_cutoff, p_batch_size) IS NOT NULL THEN
        RAISE EXCEPTION 'invalid retention parameters';
    END IF;

    v_cutoff := LEAST(p_cutoff, clock_timestamp());

    DELETE FROM public.outbox_events
    WHERE id IN (
        SELECT id
        FROM public.outbox_events
        WHERE status IN ('completed', 'dead_letter')
          AND retention_until IS NOT NULL
          AND retention_until < v_cutoff
        ORDER BY retention_until
        LIMIT p_batch_size
    );
    GET DIAGNOSTICS v_deleted = ROW_COUNT;
    RETURN v_deleted;
END;
$$;

REVOKE ALL ON FUNCTION public.purge_outbox_events(timestamptz, integer)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.purge_outbox_events(timestamptz, integer)
    TO service_role;
