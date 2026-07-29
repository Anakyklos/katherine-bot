-- 20240101000003_admission_ledger.sql
-- Atomic, server-owned admission ledger.
--
-- Global advisory transaction lock key: (1262572616, 1094995249)
-- ASCII mnemonic: "KATH" / "ADM1". The global lock is deliberate while the
-- application-wide limit is 25 reservations per rolling minute.

CREATE TABLE public.admission_reservations (
    user_id text NOT NULL,
    request_id uuid NOT NULL,
    message_hmac_sha256 text NOT NULL,
    network_hmac_sha256 text NOT NULL,
    estimated_units integer NOT NULL,
    reserved_at timestamptz NOT NULL DEFAULT clock_timestamp(),

    CONSTRAINT admission_reservations_pkey
        PRIMARY KEY (user_id, request_id),
    CONSTRAINT admission_reservations_user_id_check
        CHECK (
            char_length(user_id) BETWEEN 1 AND 128
            AND btrim(user_id) <> ''
        ),
    CONSTRAINT admission_reservations_message_hmac_check
        CHECK (message_hmac_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT admission_reservations_network_hmac_check
        CHECK (network_hmac_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT admission_reservations_estimated_units_check
        CHECK (estimated_units BETWEEN 1 AND 6000)
);

CREATE INDEX admission_reservations_user_time_idx
    ON public.admission_reservations (user_id, reserved_at DESC);
CREATE INDEX admission_reservations_network_time_idx
    ON public.admission_reservations (network_hmac_sha256, reserved_at DESC);
CREATE INDEX admission_reservations_time_idx
    ON public.admission_reservations (reserved_at DESC);

ALTER TABLE public.admission_reservations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.admission_reservations FORCE ROW LEVEL SECURITY;

REVOKE ALL PRIVILEGES ON TABLE public.admission_reservations
    FROM PUBLIC, anon, authenticated, service_role;

-- Intentionally no policies. Runtime roles cannot access the table directly.

CREATE OR REPLACE FUNCTION public.reserve_admission(
    p_user_id text,
    p_request_id uuid,
    p_message_hmac_sha256 text,
    p_network_hmac_sha256 text,
    p_estimated_units integer
)
RETURNS TABLE(decision text, retry_after_seconds integer)
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_now constant timestamptz := clock_timestamp();

    v_user_requests_per_minute constant integer := 20;
    v_network_requests_per_minute constant integer := 60;
    v_application_requests_per_minute constant integer := 25;
    v_user_requests_per_day constant integer := 200;
    v_user_estimated_units_per_day constant bigint := 250000;
    v_message_max_estimated_units constant integer := 6000;
    v_retry_minute constant integer := 60;
    v_retry_day constant integer := 86400;

    v_existing_hmac text;
    v_count bigint;
    v_sum_units bigint;
    v_inserted integer;
BEGIN
    -- Serialize every duplicate check, quota read, and reservation insert.
    PERFORM pg_catalog.pg_advisory_xact_lock(1262572616, 1094995249);

    -- Input validation is fail-closed and never persists rejected input.
    IF p_user_id IS NULL
       OR char_length(p_user_id) NOT BETWEEN 1 AND 128
       OR pg_catalog.btrim(p_user_id) = ''
       OR p_request_id IS NULL
       OR p_message_hmac_sha256 IS NULL
       OR p_message_hmac_sha256 !~ '^[0-9a-f]{64}$'
       OR p_network_hmac_sha256 IS NULL
       OR p_network_hmac_sha256 !~ '^[0-9a-f]{64}$'
       OR p_estimated_units IS NULL
       OR p_estimated_units NOT BETWEEN 1 AND v_message_max_estimated_units
    THEN
        RETURN QUERY SELECT 'invalid_admission_input'::text, 0::integer;
        RETURN;
    END IF;

    -- Duplicate/conflict semantics are scoped by user and request_id only.
    SELECT r.message_hmac_sha256
      INTO v_existing_hmac
      FROM public.admission_reservations AS r
     WHERE r.user_id = p_user_id
       AND r.request_id = p_request_id;

    IF FOUND THEN
        IF v_existing_hmac = p_message_hmac_sha256 THEN
            RETURN QUERY SELECT 'request_replay_unavailable'::text, 0::integer;
        ELSE
            RETURN QUERY SELECT 'request_id_conflict'::text, 0::integer;
        END IF;
        RETURN;
    END IF;

    SELECT count(*)
      INTO v_count
      FROM public.admission_reservations AS r
     WHERE r.user_id = p_user_id
       AND r.reserved_at > v_now - interval '60 seconds';

    IF v_count >= v_user_requests_per_minute THEN
        RETURN QUERY SELECT 'user_rate_limited'::text, v_retry_minute;
        RETURN;
    END IF;

    SELECT count(*)
      INTO v_count
      FROM public.admission_reservations AS r
     WHERE r.network_hmac_sha256 = p_network_hmac_sha256
       AND r.reserved_at > v_now - interval '60 seconds';

    IF v_count >= v_network_requests_per_minute THEN
        RETURN QUERY SELECT 'network_rate_limited'::text, v_retry_minute;
        RETURN;
    END IF;

    SELECT count(*)
      INTO v_count
      FROM public.admission_reservations AS r
     WHERE r.reserved_at > v_now - interval '60 seconds';

    IF v_count >= v_application_requests_per_minute THEN
        RETURN QUERY SELECT 'application_rate_limited'::text, v_retry_minute;
        RETURN;
    END IF;

    SELECT count(*)
      INTO v_count
      FROM public.admission_reservations AS r
     WHERE r.user_id = p_user_id
       AND r.reserved_at > v_now - interval '24 hours';

    IF v_count >= v_user_requests_per_day THEN
        RETURN QUERY SELECT 'user_daily_request_quota_exceeded'::text, v_retry_day;
        RETURN;
    END IF;

    SELECT COALESCE(sum(r.estimated_units), 0)
      INTO v_sum_units
      FROM public.admission_reservations AS r
     WHERE r.user_id = p_user_id
       AND r.reserved_at > v_now - interval '24 hours';

    IF v_sum_units + p_estimated_units > v_user_estimated_units_per_day THEN
        RETURN QUERY SELECT 'user_daily_unit_quota_exceeded'::text, v_retry_day;
        RETURN;
    END IF;

    INSERT INTO public.admission_reservations (
        user_id,
        request_id,
        message_hmac_sha256,
        network_hmac_sha256,
        estimated_units,
        reserved_at
    )
    VALUES (
        p_user_id,
        p_request_id,
        p_message_hmac_sha256,
        p_network_hmac_sha256,
        p_estimated_units,
        v_now
    )
    ON CONFLICT (user_id, request_id) DO NOTHING;

    GET DIAGNOSTICS v_inserted = ROW_COUNT;

    IF v_inserted = 1 THEN
        RETURN QUERY SELECT 'admitted'::text, 0::integer;
        RETURN;
    END IF;

    -- Defensive re-read for a concurrent administrative writer that did not
    -- participate in the advisory-lock protocol. No exception text is parsed.
    SELECT r.message_hmac_sha256
      INTO v_existing_hmac
      FROM public.admission_reservations AS r
     WHERE r.user_id = p_user_id
       AND r.request_id = p_request_id;

    IF FOUND AND v_existing_hmac = p_message_hmac_sha256 THEN
        RETURN QUERY SELECT 'request_replay_unavailable'::text, 0::integer;
        RETURN;
    END IF;

    IF FOUND THEN
        RETURN QUERY SELECT 'request_id_conflict'::text, 0::integer;
        RETURN;
    END IF;

    -- A concurrent insert followed by an administrative delete is not a normal
    -- business decision. Fail the transaction with a constant, sanitized code.
    RAISE EXCEPTION USING
        ERRCODE = '40001',
        MESSAGE = 'admission_reservation_race';
END;
$$;

REVOKE ALL PRIVILEGES ON FUNCTION public.reserve_admission(text, uuid, text, text, integer)
    FROM PUBLIC, anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.reserve_admission(text, uuid, text, text, integer)
    TO service_role;
