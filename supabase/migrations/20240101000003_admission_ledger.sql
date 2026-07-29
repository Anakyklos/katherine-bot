-- 20240101000003_admission_ledger.sql
-- Atomic PostgreSQL admission ledger for request rate limiting,
-- duplicate detection, and conflict detection.
--
-- Creates:
--   public.admission_reservations  — table with PK, CHECK, indices, RLS
--   public.reserve_admission(...)  — SECURITY DEFINER RPC
--
-- The table is accessible only through the RPC; no direct grants exist
-- for any role (including service_role).  RLS + FORCE RLS block all
-- PostgREST access.

-- =================================================================
-- 1. Create admission_reservations table
-- =================================================================
CREATE TABLE IF NOT EXISTS public.admission_reservations (
    user_id              text        NOT NULL,
    request_id           uuid        NOT NULL,
    message_hmac_sha256  text        NOT NULL,
    network_hmac_sha256  text        NOT NULL,
    estimated_units      integer     NOT NULL,
    reserved_at          timestamptz NOT NULL DEFAULT clock_timestamp(),

    CONSTRAINT admission_reservations_pkey
        PRIMARY KEY (user_id, request_id),
    CONSTRAINT admission_reservations_user_id_check
        CHECK (user_id IS NOT NULL AND user_id <> '' AND length(user_id) <= 128),
    CONSTRAINT admission_reservations_message_hmac_check
        CHECK (message_hmac_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT admission_reservations_network_hmac_check
        CHECK (network_hmac_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT admission_reservations_estimated_units_check
        CHECK (estimated_units >= 1 AND estimated_units <= 6000)
);

-- =================================================================
-- 2. Create indices for rate-limit window queries
-- =================================================================
CREATE INDEX IF NOT EXISTS admission_reservations_user_time_idx
    ON public.admission_reservations (user_id, reserved_at);
CREATE INDEX IF NOT EXISTS admission_reservations_network_time_idx
    ON public.admission_reservations (network_hmac_sha256, reserved_at);
CREATE INDEX IF NOT EXISTS admission_reservations_time_idx
    ON public.admission_reservations (reserved_at);

-- =================================================================
-- 3. RLS + FORCE RLS — no PostgREST access without explicit grants
-- =================================================================
ALTER TABLE public.admission_reservations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.admission_reservations FORCE ROW LEVEL SECURITY;

-- =================================================================
-- 4. Revoke ALL privileges from all built-in roles
-- =================================================================
REVOKE ALL PRIVILEGES ON TABLE public.admission_reservations FROM PUBLIC;
REVOKE ALL PRIVILEGES ON TABLE public.admission_reservations FROM anon;
REVOKE ALL PRIVILEGES ON TABLE public.admission_reservations FROM authenticated;
REVOKE ALL PRIVILEGES ON TABLE public.admission_reservations FROM service_role;

-- =================================================================
-- 5. No policies — table is completely inaccessible via PostgREST
-- =================================================================
-- Intentionally no CREATE POLICY statements.  RLS + FORCE RLS + no
-- grants = every query (SELECT, INSERT, UPDATE, DELETE) is rejected
-- for all roles, including service_role.

-- =================================================================
-- 6. Create reserve_admission RPC (SECURITY DEFINER)
-- =================================================================
CREATE OR REPLACE FUNCTION public.reserve_admission(
    p_user_id text,
    p_request_id uuid,
    p_message_hmac_sha256 text,
    p_network_hmac_sha256 text,
    p_estimated_units integer
)
RETURNS TABLE(decision text, retry_after_seconds integer)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_now           timestamptz;
    v_existing_user_id text;
    v_existing_hmac text;
    v_count         integer;
    v_oldest        timestamptz;
    v_sum_units     bigint;
    v_retry         integer;
BEGIN
    -- Single clock reading for the entire execution
    v_now := clock_timestamp();

    -- =============================================================
    -- 6a. Validate inputs
    -- =============================================================
    IF p_user_id IS NULL OR p_user_id = '' OR length(p_user_id) > 128 THEN
        RETURN QUERY SELECT 'invalid_admission_input'::text, 0::integer;
        RETURN;
    END IF;

    IF p_request_id IS NULL THEN
        RETURN QUERY SELECT 'invalid_admission_input'::text, 0::integer;
        RETURN;
    END IF;

    IF p_message_hmac_sha256 IS NULL
       OR length(p_message_hmac_sha256) != 64
       OR p_message_hmac_sha256 !~ '^[0-9a-f]{64}$'
    THEN
        RETURN QUERY SELECT 'invalid_admission_input'::text, 0::integer;
        RETURN;
    END IF;

    IF p_network_hmac_sha256 IS NULL
       OR length(p_network_hmac_sha256) != 64
       OR p_network_hmac_sha256 !~ '^[0-9a-f]{64}$'
    THEN
        RETURN QUERY SELECT 'invalid_admission_input'::text, 0::integer;
        RETURN;
    END IF;

    IF p_estimated_units IS NULL
       OR p_estimated_units < 1
       OR p_estimated_units > 6000
    THEN
        RETURN QUERY SELECT 'invalid_admission_input'::text, 0::integer;
        RETURN;
    END IF;

    -- =============================================================
    -- 6b. Advisory lock per user (serialize concurrent requests)
    -- =============================================================
    PERFORM pg_advisory_xact_lock(hashtext(p_user_id)::bigint);

    -- =============================================================
    -- 6c. Exact replay: same (user_id, request_id, message_hmac)
    -- =============================================================
    SELECT 1 INTO v_existing_user_id
    FROM public.admission_reservations
    WHERE user_id = p_user_id
      AND request_id = p_request_id
      AND message_hmac_sha256 = p_message_hmac_sha256
    LIMIT 1;

    IF FOUND THEN
        RETURN QUERY SELECT 'request_replay_unavailable'::text, 0::integer;
        RETURN;
    END IF;

    -- =============================================================
    -- 6d. Duplicate message HMAC (same content, different request)
    --      → admitted without consuming new quota
    -- =============================================================
    SELECT message_hmac_sha256 INTO v_existing_hmac
    FROM public.admission_reservations
    WHERE user_id = p_user_id
      AND message_hmac_sha256 = p_message_hmac_sha256
    LIMIT 1;

    IF FOUND THEN
        RETURN QUERY SELECT 'admitted'::text, 0::integer;
        RETURN;
    END IF;

    -- =============================================================
    -- 6e. Request ID conflict (same request_id, different HMAC)
    -- =============================================================
    SELECT 1 INTO v_existing_user_id
    FROM public.admission_reservations
    WHERE user_id = p_user_id
      AND request_id = p_request_id
    LIMIT 1;

    IF FOUND THEN
        RETURN QUERY SELECT 'request_id_conflict'::text, 0::integer;
        RETURN;
    END IF;

    -- =============================================================
    -- 6f. User rate limit: 20 requests per 60-second sliding window
    -- =============================================================
    SELECT count(*), min(reserved_at) INTO v_count, v_oldest
    FROM public.admission_reservations
    WHERE user_id = p_user_id
      AND reserved_at > v_now - interval '60 seconds';

    IF v_count >= 20 THEN
        v_retry := GREATEST(
            CEIL(EXTRACT(EPOCH FROM (v_oldest + interval '60 seconds' - v_now)))::integer,
            1
        );
        RETURN QUERY SELECT 'user_rate_limited'::text, v_retry;
        RETURN;
    END IF;

    -- =============================================================
    -- 6g. Network rate limit: 60 requests per 60-second sliding window
    -- =============================================================
    SELECT count(*), min(reserved_at) INTO v_count, v_oldest
    FROM public.admission_reservations
    WHERE network_hmac_sha256 = p_network_hmac_sha256
      AND reserved_at > v_now - interval '60 seconds';

    IF v_count >= 60 THEN
        v_retry := GREATEST(
            CEIL(EXTRACT(EPOCH FROM (v_oldest + interval '60 seconds' - v_now)))::integer,
            1
        );
        RETURN QUERY SELECT 'network_rate_limited'::text, v_retry;
        RETURN;
    END IF;

    -- =============================================================
    -- 6h. Application rate limit: 25 requests per 60-second sliding
    --     window (global, across all users)
    -- =============================================================
    SELECT count(*), min(reserved_at) INTO v_count, v_oldest
    FROM public.admission_reservations
    WHERE reserved_at > v_now - interval '60 seconds';

    IF v_count >= 25 THEN
        v_retry := GREATEST(
            CEIL(EXTRACT(EPOCH FROM (v_oldest + interval '60 seconds' - v_now)))::integer,
            1
        );
        RETURN QUERY SELECT 'application_rate_limited'::text, v_retry;
        RETURN;
    END IF;

    -- =============================================================
    -- 6i. User daily request quota: 200 per 24-hour sliding window
    -- =============================================================
    SELECT count(*), min(reserved_at) INTO v_count, v_oldest
    FROM public.admission_reservations
    WHERE user_id = p_user_id
      AND reserved_at > v_now - interval '24 hours';

    IF v_count >= 200 THEN
        v_retry := GREATEST(
            CEIL(EXTRACT(EPOCH FROM (v_oldest + interval '24 hours' - v_now)))::integer,
            1
        );
        RETURN QUERY SELECT 'user_daily_request_quota_exceeded'::text, v_retry;
        RETURN;
    END IF;

    -- =============================================================
    -- 6j. User daily unit quota: 250,000 estimated units per 24 hours
    -- =============================================================
    SELECT COALESCE(sum(estimated_units), 0) INTO v_sum_units
    FROM public.admission_reservations
    WHERE user_id = p_user_id
      AND reserved_at > v_now - interval '24 hours';

    IF v_sum_units + p_estimated_units > 250000 THEN
        RETURN QUERY SELECT 'user_daily_unit_quota_exceeded'::text, 1;
        RETURN;
    END IF;

    -- =============================================================
    -- 6k. Insert reservation and return admitted
    -- =============================================================
    INSERT INTO public.admission_reservations (
        user_id, request_id, message_hmac_sha256, network_hmac_sha256,
        estimated_units, reserved_at
    ) VALUES (
        p_user_id, p_request_id, p_message_hmac_sha256, p_network_hmac_sha256,
        p_estimated_units, v_now
    );

    RETURN QUERY SELECT 'admitted'::text, 0::integer;
END;
$$;

-- =================================================================
-- 7. Grant EXECUTE on RPC only to service_role
-- =================================================================
REVOKE ALL PRIVILEGES ON FUNCTION public.reserve_admission(text, uuid, text, text, integer) FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION public.reserve_admission(text, uuid, text, text, integer) FROM anon;
REVOKE ALL PRIVILEGES ON FUNCTION public.reserve_admission(text, uuid, text, text, integer) FROM authenticated;
REVOKE ALL PRIVILEGES ON FUNCTION public.reserve_admission(text, uuid, text, text, integer) FROM service_role;
GRANT EXECUTE ON FUNCTION public.reserve_admission(text, uuid, text, text, integer) TO service_role;
