-- 20260810120000_account_deletion_ledger.sql
-- Durable account deletion ledger (#324).
--
-- Purely additive, fail-closed migration. Creates the server-owned
-- account deletion job ledger and the privileged RPC boundary for the
-- future account deletion pipeline. This PR deliberately stops at the
-- database: no HTTP endpoint, no Supabase Auth Admin call, no worker/CLI.
--
-- Why a ledger (root cause)
-- =========================
-- Deleting an account crosses PostgreSQL and Supabase Auth. There is no
-- distributed transaction that makes both steps atomically equivalent, so
-- the mandatory architecture is:
--
--   1. register a durable tombstone (this ledger);
--   2. purge the PostgreSQL data in ONE transaction (this migration);
--   3. only in a later task (#325), and only after the commit above is
--      confirmed, delete the Auth user;
--   4. allow safe retry after a crash (leases + attempts + next_attempt_at).
--
-- The tombstone must survive the ``profiles`` DELETE, therefore the job
-- table has NO foreign key to ``profiles`` (identity is a server-derived
-- HMAC reference plus the raw user_id only while the job is active).
--
-- State machine
-- =============
--   pending     -> registered, not yet claimed
--   processing  -> claimed by exactly one worker (lease owner + expiry)
--   failed      -> a worker reported a sanitized failure; retry eligible
--                  after next_attempt_at (backoff derived from attempts)
--   completed   -> DB purge committed (db_purged_at) AND Auth deletion
--                  confirmed later (#325); user_id minimized to NULL
--
-- Constraints reject incoherent states (details in the table DDL):
--   * only processing carries a lease; pending/completed/failed never do;
--   * completed <=> db_purged_at NOT NULL AND completed_at NOT NULL;
--   * completed requires user_id IS NULL (identity minimization);
--   * failed requires a sanitized error_code;
--   * attempts is never negative; identifiers use strict allowlists.
--
-- Concurrency (binding decision, mirrors commit_turn / #314)
-- ==========================================================
--   * every purge acquires the SAME per-user advisory transaction lock as
--     the turn commit: pg_advisory_xact_lock(hashtextextended(user_id, 0)).
--     A concurrent turn commit of the same user can never interleave with
--     the deletion; distinct users are never globally serialized.
--   * lease claims use SELECT ... FOR UPDATE SKIP LOCKED: two workers can
--     never own the same job simultaneously (no process-local lock).
--   * ownership is validated on every job-scoped RPC (worker id + unexpired
--     lease), so an old worker cannot finalize after losing the lease.
--
-- Purge atomicity / commit invariant
-- ==================================
--   * all user rows are deleted in ONE transaction;
--   * db_purged_at is set in the SAME transaction that completed the purge
--     (never before, never after a separate COMMIT);
--   * any failure rolls back every delete of that attempt;
--   * a repeated purge after the data is already gone is a safe no-op:
--     db_purged_at is authoritative and no delete depends on the row
--     still existing (replay returns already_purged without re-running).
--
-- Retention (completed tombstones)
-- ================================
--   * account_deletion_purge_completed deletes ONLY completed jobs older
--     than the horizon; the cutoff is clamped by the database against
--     clock_timestamp() - 30 days (authoritative PostgreSQL time), so a
--     fast process clock can never advance deletion;
--   * pending/processing/failed jobs are NEVER removed by age;
--   * no scheduler is created here; the RPC reuses the operational
--     cleanup model of #316 and the future worker may call it.
--
-- Security posture (binding decisions)
-- ====================================
--   * account_deletion_jobs is RLS + FORCE RLS with zero policies and NO
--     table grants (not even service_role): the RPCs are the only path.
--   * every runtime RPC is SECURITY DEFINER with SET search_path = '' and
--     EXECUTE granted to service_role only (revoked from PUBLIC, anon and
--     authenticated). No dynamic SQL, no raw exception text, no parameters
--     in logs, sanitized jsonb envelopes only.
--   * internal helpers have NO grants at all (owner postgres only).
--   * identity comes ONLY from p_authenticated_user_id (server-side
--     boundary). The persistent reference is an HMAC-SHA256 (lowercase
--     hex, 64 chars) under a DEDICATED account-deletion domain, distinct
--     from the message/network/correlation/user-reference domains, so it
--     can never be correlated with USER_REFERENCE_DOMAIN values. The
--     raw user_id is removed (NULL) once the job is completed.
--   * idempotency: (user_ref_hmac_sha256, operation_id) is UNIQUE; the
--     same pair with the same intent fingerprint is an exact replay (no
--     second job); the same pair with a divergent fingerprint is a
--     sanitized operation_conflict. Different users never collide because
--     the reference is a domain-separated HMAC of the user identity.

-- =================================================================
-- 0. PREFLIGHT: fail closed on missing dependencies and drift
-- =================================================================
DO $$
BEGIN
    -- Drift: objects created by THIS migration must not exist yet.
    IF EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relname = 'account_deletion_jobs'
    ) THEN
        RAISE EXCEPTION 'Cannot apply account deletion ledger: account_deletion_jobs already exists (unexpected drift)'
            USING ERRCODE = '23514';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
          AND p.proname IN (
              'account_deletion_request', 'account_deletion_has_tombstone',
              'account_deletion_acquire_lease', 'account_deletion_purge',
              'account_deletion_record_failure', 'account_deletion_record_retry',
              'account_deletion_finalize', 'account_deletion_purge_completed',
              'account_deletion_validation_error', 'account_deletion_intent_fingerprint_sha256'
          )
    ) THEN
        RAISE EXCEPTION 'Cannot apply account deletion ledger: account deletion functions already exist (unexpected drift)'
            USING ERRCODE = '23514';
    END IF;

    -- Required tables: EVERY purge target must exist. Each check is
    -- explicit so a single existing table can never mask another missing
    -- one (same fail-closed pattern as #314/#316 preflights).
    IF pg_catalog.to_regclass('public.chat_logs') IS NULL THEN
        RAISE EXCEPTION 'Missing dependency: public.chat_logs' USING ERRCODE = '23514';
    END IF;
    IF pg_catalog.to_regclass('public.memories') IS NULL THEN
        RAISE EXCEPTION 'Missing dependency: public.memories' USING ERRCODE = '23514';
    END IF;
    IF pg_catalog.to_regclass('public.archival_extractions') IS NULL THEN
        RAISE EXCEPTION 'Missing dependency: public.archival_extractions' USING ERRCODE = '23514';
    END IF;
    IF pg_catalog.to_regclass('public.turn_requests') IS NULL THEN
        RAISE EXCEPTION 'Missing dependency: public.turn_requests' USING ERRCODE = '23514';
    END IF;
    IF pg_catalog.to_regclass('public.outbox_events') IS NULL THEN
        RAISE EXCEPTION 'Missing dependency: public.outbox_events' USING ERRCODE = '23514';
    END IF;
    IF pg_catalog.to_regclass('public.admission_reservations') IS NULL THEN
        RAISE EXCEPTION 'Missing dependency: public.admission_reservations' USING ERRCODE = '23514';
    END IF;
    IF pg_catalog.to_regclass('public.privacy_operations') IS NULL THEN
        RAISE EXCEPTION 'Missing dependency: public.privacy_operations' USING ERRCODE = '23514';
    END IF;
    IF pg_catalog.to_regclass('public.profiles') IS NULL THEN
        RAISE EXCEPTION 'Missing dependency: public.profiles' USING ERRCODE = '23514';
    END IF;

    -- Drift of the per-user lock contract: the turn commit boundary must
    -- exist (jsonb_snapshot_contract from #271 is the turn-commit contract
    -- dependency; profiles.revision is required by the privacy chain).
    IF NOT EXISTS (
        SELECT 1
        FROM pg_attribute a
        WHERE a.attrelid = 'public.profiles'::regclass
          AND a.attname = 'revision'
    ) THEN
        RAISE EXCEPTION 'Missing dependency: profiles.revision column' USING ERRCODE = '23514';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
          AND p.proname = 'jsonb_snapshot_contract'
    ) THEN
        RAISE EXCEPTION 'Missing dependency: public.jsonb_snapshot_contract (turn commit contract)' USING ERRCODE = '23514';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
          AND p.proname = 'commit_turn'
    ) THEN
        RAISE EXCEPTION 'Missing dependency: public.commit_turn (per-user lock contract)' USING ERRCODE = '23514';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_extension e
        WHERE e.extname = 'pgcrypto'
    ) THEN
        RAISE EXCEPTION 'Missing dependency: pgcrypto extension (intent fingerprint)' USING ERRCODE = '23514';
    END IF;
END $$;

-- =================================================================
-- 1. Durable account deletion job ledger
-- =================================================================
CREATE TABLE public.account_deletion_jobs (
    job_id uuid NOT NULL DEFAULT gen_random_uuid(),
    operation_id uuid NOT NULL,
    -- Raw user identity: present ONLY while the job still needs to run the
    -- DB purge / future Auth deletion; NULL once completed (minimization).
    user_id text,
    -- Server-derived, non-reversible reference (HMAC-SHA256, dedicated
    -- account-deletion domain). Persists after completion for bounded
    -- audit and idempotency. Never client-supplied.
    user_ref_hmac_sha256 text NOT NULL,
    -- SHA-256 fingerprint of the canonical intent payload; binds an
    -- operation_id to its exact intent (replay vs conflict).
    intent_fingerprint_sha256 text NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0,
    lease_owner text,
    lease_expires_at timestamptz,
    next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    db_purged_at timestamptz,
    error_code text,
    requested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,

    CONSTRAINT account_deletion_jobs_pkey
        PRIMARY KEY (job_id),
    -- Idempotency key: one job per (sanitized identity, operation_id).
    -- Different users never collide because the reference is a
    -- domain-separated HMAC of the identity. A divergent fingerprint is
    -- detected by the request RPC (sanitized conflict) before any insert.
    CONSTRAINT account_deletion_jobs_idempotency_key
        UNIQUE (user_ref_hmac_sha256, operation_id),
    CONSTRAINT account_deletion_jobs_user_id_check
        CHECK (
            user_id IS NULL
            OR (char_length(user_id) BETWEEN 1 AND 128 AND btrim(user_id) <> '')
        ),
    CONSTRAINT account_deletion_jobs_user_ref_check
        CHECK (user_ref_hmac_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT account_deletion_jobs_fingerprint_check
        CHECK (intent_fingerprint_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT account_deletion_jobs_status_check
        CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
    CONSTRAINT account_deletion_jobs_attempts_check
        CHECK (attempts >= 0 AND attempts <= 1000),
    CONSTRAINT account_deletion_jobs_lease_owner_check
        CHECK (lease_owner IS NULL OR lease_owner ~ '^[A-Za-z0-9_.:-]{1,64}$'),
    -- Lease fields travel together.
    CONSTRAINT account_deletion_jobs_lease_pair_check
        CHECK (
            (lease_owner IS NULL AND lease_expires_at IS NULL)
            OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
        ),
    -- Only processing carries a lease; pending/completed/failed never do.
    CONSTRAINT account_deletion_jobs_lease_state_check
        CHECK (
            (status = 'processing' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
            OR (status <> 'processing' AND lease_owner IS NULL AND lease_expires_at IS NULL)
        ),
    -- Raw identity is required while active, removed once completed.
    CONSTRAINT account_deletion_jobs_user_id_state_check
        CHECK (
            (status = 'completed' AND user_id IS NULL)
            OR (status <> 'completed' AND user_id IS NOT NULL)
        ),
    -- db_purged_at is set exactly when the purge transaction committed.
    -- It may be non-NULL while the job is processing (purge committed,
    -- awaiting the future Auth deletion confirmation in #325) and is
    -- REQUIRED for completed. pending/failed never carry it.
    CONSTRAINT account_deletion_jobs_db_purged_state_check
        CHECK (
            (status = 'completed' AND db_purged_at IS NOT NULL)
            OR status = 'processing'
            OR (status IN ('pending', 'failed') AND db_purged_at IS NULL)
        ),
    CONSTRAINT account_deletion_jobs_completed_at_state_check
        CHECK ((status = 'completed') = (completed_at IS NOT NULL)),
    CONSTRAINT account_deletion_jobs_error_code_check
        CHECK (error_code IS NULL OR error_code ~ '^[a-z0-9_]{1,64}$'),
    -- failed jobs must carry a sanitized error_code.
    CONSTRAINT account_deletion_jobs_failed_error_check
        CHECK (status <> 'failed' OR error_code IS NOT NULL)
);

-- Claim scan: eligible jobs ordered by next_attempt_at.
CREATE INDEX account_deletion_jobs_status_next_attempt_idx
    ON public.account_deletion_jobs (status, next_attempt_at);

-- Tombstone lookup by server-derived reference.
CREATE INDEX account_deletion_jobs_user_ref_idx
    ON public.account_deletion_jobs (user_ref_hmac_sha256, requested_at DESC);

-- Retention scan: only completed jobs are ever aged out.
CREATE INDEX account_deletion_jobs_completed_at_idx
    ON public.account_deletion_jobs (completed_at)
    WHERE status = 'completed';

ALTER TABLE public.account_deletion_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.account_deletion_jobs FORCE ROW LEVEL SECURITY;

-- Server-owned ledger: no role (not even service_role) gets table access.
REVOKE ALL PRIVILEGES ON TABLE public.account_deletion_jobs
    FROM PUBLIC, anon, authenticated, service_role;

-- Intentionally no policies.

-- =================================================================
-- 2. Intent fingerprint helper (internal, no grants)
-- =================================================================
-- Deterministic SHA-256 of the canonical jsonb serialization, identical in
-- spirit to privacy_operation_payload_sha256 (#314).
CREATE OR REPLACE FUNCTION public.account_deletion_intent_fingerprint_sha256(
    p_payload jsonb
) RETURNS text
LANGUAGE sql
IMMUTABLE
SET search_path = extensions
AS $$
    SELECT pg_catalog.encode(extensions.digest(p_payload::text, 'sha256'), 'hex');
$$;

REVOKE ALL ON FUNCTION public.account_deletion_intent_fingerprint_sha256(jsonb)
    FROM PUBLIC, anon, authenticated, service_role;

-- =================================================================
-- 3. Base validation helper (internal, no grants)
-- =================================================================
-- Returns a sanitized error envelope or NULL. Mirrors the persistent
-- ledger constraints exactly so invalid input fails BEFORE any lock,
-- mutation or ledger row, never via a CHECK violation.
CREATE OR REPLACE FUNCTION public.account_deletion_validation_error(
    p_authenticated_user_id text,
    p_user_ref_hmac_sha256 text,
    p_operation_id uuid,
    p_intent_fingerprint_sha256 text
) RETURNS jsonb
LANGUAGE plpgsql
IMMUTABLE
SET search_path = ''
AS $$
BEGIN
    IF p_authenticated_user_id IS NULL
       OR pg_catalog.char_length(p_authenticated_user_id) NOT BETWEEN 1 AND 128
       OR pg_catalog.btrim(p_authenticated_user_id) = '' THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'validation_failed',
                'message', 'authenticated_user_id is invalid'
            )
        );
    END IF;
    IF p_user_ref_hmac_sha256 IS NULL
       OR p_user_ref_hmac_sha256 !~ '^[0-9a-f]{64}$' THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'validation_failed',
                'message', 'user_ref_hmac_sha256 must be 64 lowercase hex characters'
            )
        );
    END IF;
    IF p_operation_id IS NULL THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'validation_failed',
                'message', 'operation_id is required'
            )
        );
    END IF;
    IF p_intent_fingerprint_sha256 IS NULL
       OR p_intent_fingerprint_sha256 !~ '^[0-9a-f]{64}$' THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'validation_failed',
                'message', 'intent_fingerprint_sha256 must be 64 lowercase hex characters'
            )
        );
    END IF;
    RETURN NULL;
END;
$$;

REVOKE ALL ON FUNCTION public.account_deletion_validation_error(text, text, uuid, text)
    FROM PUBLIC, anon, authenticated, service_role;

-- =================================================================
-- 4. account_deletion_request
-- =================================================================
-- Registers (or replays) an account deletion request. Server-side
-- boundary only: identity, reference, operation_id and fingerprint are
-- produced by the trusted backend, never by an HTTP client.
--
--   * same (ref, operation_id) + same fingerprint -> exact replay: the
--     existing job state is returned, no second job is created;
--   * same pair + divergent fingerprint -> sanitized operation_conflict;
--   * different users never collide (ref is a domain-separated HMAC).
CREATE OR REPLACE FUNCTION public.account_deletion_request(
    p_authenticated_user_id text,
    p_user_ref_hmac_sha256 text,
    p_operation_id uuid,
    p_intent_fingerprint_sha256 text
) RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_validation_error jsonb;
    v_job public.account_deletion_jobs%ROWTYPE;
    v_job_id uuid;
BEGIN
    v_validation_error := public.account_deletion_validation_error(
        p_authenticated_user_id,
        p_user_ref_hmac_sha256,
        p_operation_id,
        p_intent_fingerprint_sha256
    );
    IF v_validation_error IS NOT NULL THEN
        RETURN v_validation_error;
    END IF;

    -- Serialize request against concurrent purge/finalize of the same user.
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(p_authenticated_user_id, 0)
    );

    SELECT * INTO v_job
    FROM public.account_deletion_jobs
    WHERE user_ref_hmac_sha256 = p_user_ref_hmac_sha256
      AND operation_id = p_operation_id;

    IF FOUND THEN
        IF v_job.intent_fingerprint_sha256 = p_intent_fingerprint_sha256 THEN
            -- Exact replay: return the current sanitized state.
            RETURN jsonb_build_object(
                'status', 'replay',
                'job_id', v_job.job_id::text,
                'job_status', v_job.status,
                'db_purged_at', v_job.db_purged_at,
                'completed_at', v_job.completed_at
            );
        END IF;
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'operation_conflict',
                'message', 'operation_id already used with a different intent'
            )
        );
    END IF;

    INSERT INTO public.account_deletion_jobs (
        operation_id, user_id, user_ref_hmac_sha256, intent_fingerprint_sha256,
        status, attempts, next_attempt_at
    ) VALUES (
        p_operation_id, p_authenticated_user_id, p_user_ref_hmac_sha256,
        p_intent_fingerprint_sha256, 'pending', 0, clock_timestamp()
    )
    RETURNING job_id INTO v_job_id;

    RETURN jsonb_build_object(
        'status', 'created',
        'job_id', v_job_id::text,
        'job_status', 'pending'
    );
EXCEPTION
    WHEN OTHERS THEN
        RAISE EXCEPTION 'persistence error' USING ERRCODE = 'P0001';
END;
$$;

REVOKE ALL ON FUNCTION public.account_deletion_request(text, text, uuid, text)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.account_deletion_request(text, text, uuid, text)
    TO service_role;

-- =================================================================
-- 5. account_deletion_has_tombstone
-- =================================================================
-- Sanitized lookup by server-derived reference: reports whether a
-- blocking tombstone exists and its current status. Read-only, no lock.
CREATE OR REPLACE FUNCTION public.account_deletion_has_tombstone(
    p_user_ref_hmac_sha256 text
) RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_status text;
BEGIN
    IF p_user_ref_hmac_sha256 IS NULL
       OR p_user_ref_hmac_sha256 !~ '^[0-9a-f]{64}$' THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'validation_failed',
                'message', 'user_ref_hmac_sha256 must be 64 lowercase hex characters'
            )
        );
    END IF;

    SELECT status INTO v_status
    FROM public.account_deletion_jobs
    WHERE user_ref_hmac_sha256 = p_user_ref_hmac_sha256
    ORDER BY requested_at DESC
    LIMIT 1;

    IF v_status IS NULL THEN
        RETURN jsonb_build_object('exists', false, 'status', NULL);
    END IF;
    RETURN jsonb_build_object('exists', true, 'status', v_status);
EXCEPTION
    WHEN OTHERS THEN
        RAISE EXCEPTION 'persistence error' USING ERRCODE = 'P0001';
END;
$$;

REVOKE ALL ON FUNCTION public.account_deletion_has_tombstone(text)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.account_deletion_has_tombstone(text)
    TO service_role;

-- =================================================================
-- 6. account_deletion_acquire_lease
-- =================================================================
-- Claims at most one eligible job for a worker. Real PostgreSQL row-level
-- mechanism: FOR UPDATE SKIP LOCKED guarantees two workers can never own
-- the same job simultaneously. Eligible:
--   * pending with next_attempt_at <= now;
--   * failed with next_attempt_at <= now (retry after backoff);
--   * processing whose lease has EXPIRED (worker died -> recoverable).
-- Each claim increments attempts (retries are represented in the DB).
CREATE OR REPLACE FUNCTION public.account_deletion_acquire_lease(
    p_worker_id text,
    p_lease_seconds integer,
    p_max_batch integer
) RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_job public.account_deletion_jobs%ROWTYPE;
    v_now timestamptz := clock_timestamp();
BEGIN
    IF p_worker_id IS NULL OR p_worker_id !~ '^[A-Za-z0-9_.:-]{1,64}$' THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'validation_failed',
                'message', 'worker_id is invalid'
            )
        );
    END IF;
    IF p_lease_seconds IS NULL OR p_lease_seconds NOT BETWEEN 1 AND 3600 THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'validation_failed',
                'message', 'lease_seconds must be between 1 and 3600'
            )
        );
    END IF;
    IF p_max_batch IS NULL OR p_max_batch NOT BETWEEN 1 AND 1000 THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'validation_failed',
                'message', 'max_batch must be between 1 and 1000'
            )
        );
    END IF;

    SELECT * INTO v_job
    FROM public.account_deletion_jobs
    WHERE (
            (status IN ('pending', 'failed') AND next_attempt_at <= v_now)
            OR (status = 'processing' AND lease_expires_at < v_now)
          )
    ORDER BY next_attempt_at
    LIMIT 1
    FOR UPDATE SKIP LOCKED;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('found', false);
    END IF;

    UPDATE public.account_deletion_jobs
    SET status = 'processing',
        lease_owner = p_worker_id,
        lease_expires_at = v_now + make_interval(secs => p_lease_seconds),
        attempts = v_job.attempts + 1,
        error_code = NULL,
        updated_at = v_now
    WHERE job_id = v_job.job_id
    RETURNING
        job_id, user_id, user_ref_hmac_sha256, operation_id, status,
        lease_owner, lease_expires_at, attempts, db_purged_at,
        intent_fingerprint_sha256
    INTO
        v_job.job_id, v_job.user_id, v_job.user_ref_hmac_sha256, v_job.operation_id,
        v_job.status, v_job.lease_owner, v_job.lease_expires_at, v_job.attempts,
        v_job.db_purged_at, v_job.intent_fingerprint_sha256;

    RETURN jsonb_build_object(
        'found', true,
        'job_id', v_job.job_id::text,
        'user_id', v_job.user_id,
        'user_ref_hmac_sha256', v_job.user_ref_hmac_sha256,
        'operation_id', v_job.operation_id::text,
        'status', v_job.status,
        'lease_owner', v_job.lease_owner,
        'lease_expires_at', v_job.lease_expires_at,
        'attempts', v_job.attempts,
        'db_purged_at', v_job.db_purged_at,
        'intent_fingerprint_sha256', v_job.intent_fingerprint_sha256
    );
EXCEPTION
    WHEN OTHERS THEN
        RAISE EXCEPTION 'persistence error' USING ERRCODE = 'P0001';
END;
$$;

REVOKE ALL ON FUNCTION public.account_deletion_acquire_lease(text, integer, integer)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.account_deletion_acquire_lease(text, integer, integer)
    TO service_role;

-- =================================================================
-- 7. Ownership validation helper (internal, no grants)
-- =================================================================
-- Enforces that a job-scoped RPC is only accepted from the worker that
-- currently owns an unexpired lease. A worker that lost its lease (or
-- never had it) can never purge, fail, retry or finalize the job.
CREATE OR REPLACE FUNCTION public.account_deletion_assert_owner(
    p_job_id uuid,
    p_worker_id text
) RETURNS public.account_deletion_jobs
LANGUAGE plpgsql
VOLATILE
SET search_path = ''
AS $$
DECLARE
    v_job public.account_deletion_jobs%ROWTYPE;
BEGIN
    IF p_job_id IS NULL OR p_worker_id IS NULL
       OR p_worker_id !~ '^[A-Za-z0-9_.:-]{1,64}$' THEN
        RAISE EXCEPTION 'invalid account deletion job parameters'
            USING ERRCODE = 'P0001';
    END IF;

    SELECT * INTO v_job
    FROM public.account_deletion_jobs
    WHERE job_id = p_job_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'account deletion job not found'
            USING ERRCODE = 'P0001';
    END IF;

    IF v_job.status <> 'processing'
       OR v_job.lease_owner IS DISTINCT FROM p_worker_id
       OR v_job.lease_expires_at IS NULL
       OR v_job.lease_expires_at <= clock_timestamp() THEN
        RAISE EXCEPTION 'account deletion lease lost'
            USING ERRCODE = 'P0001';
    END IF;

    RETURN v_job;
END;
$$;

REVOKE ALL ON FUNCTION public.account_deletion_assert_owner(uuid, text)
    FROM PUBLIC, anon, authenticated, service_role;

-- =================================================================
-- 8. account_deletion_purge
-- =================================================================
-- Executes the full PostgreSQL purge of one user in ONE transaction:
--   1. ownership check (lease) + intent fingerprint check;
--   2. per-user advisory lock (same boundary as commit_turn / #314);
--   3. explicit deletes in FK-safe order (no assumed cascade);
--   4. db_purged_at is set in the SAME transaction (commit invariant).
-- Any failure rolls back every delete of this attempt. A repeated purge
-- after the data is already gone (crash after DB commit, before Auth) is
-- a safe replay: db_purged_at is authoritative and no delete depends on
-- the row existing. The job/tombstone is NEVER deleted by the purge.
--
-- FK-safe order (analyzed from the migrations):
--   outbox_events (FK profiles CASCADE, turn_requests CASCADE)
--   turn_requests (FK profiles CASCADE; refs chat_logs SET NULL)
--   archival_extractions (FK profiles CASCADE, chat_logs CASCADE)
--   memories (FK profiles NO ACTION)          -- must precede profiles
--   chat_logs (FK profiles NO ACTION)         -- must precede profiles
--   admission_reservations (no FK)
--   privacy_operations (no FK)
--   profiles (last: snapshots, persona, user_profile)
CREATE OR REPLACE FUNCTION public.account_deletion_purge(
    p_job_id uuid,
    p_worker_id text,
    p_intent_fingerprint_sha256 text
) RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_job public.account_deletion_jobs;
    v_count_outbox bigint := 0;
    v_count_turn_requests bigint := 0;
    v_count_archival bigint := 0;
    v_count_memories bigint := 0;
    v_count_chat_logs bigint := 0;
    v_count_admission bigint := 0;
    v_count_privacy bigint := 0;
    v_count_profiles bigint := 0;
    v_now timestamptz := clock_timestamp();
BEGIN
    IF p_job_id IS NULL OR p_intent_fingerprint_sha256 IS NULL
       OR p_intent_fingerprint_sha256 !~ '^[0-9a-f]{64}$' THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'validation_failed',
                'message', 'purge parameters are invalid'
            )
        );
    END IF;

    v_job := public.account_deletion_assert_owner(p_job_id, p_worker_id);

    -- The worker must present the same intent fingerprint bound to the
    -- job; a divergent fingerprint means a misdirected worker.
    IF v_job.intent_fingerprint_sha256 IS DISTINCT FROM p_intent_fingerprint_sha256 THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'operation_conflict',
                'message', 'intent fingerprint does not match the job'
            )
        );
    END IF;

    -- Idempotent replay: the purge already committed. Never re-delete,
    -- never touch the tombstone. Returns the same stable envelope shape
    -- (zero counts) as a fresh purge.
    IF v_job.db_purged_at IS NOT NULL THEN
        RETURN jsonb_build_object(
            'status', 'already_purged',
            'job_id', v_job.job_id::text,
            'db_purged_at', v_job.db_purged_at,
            'counts', jsonb_build_object(
                'outbox_events', 0,
                'turn_requests', 0,
                'archival_extractions', 0,
                'memories', 0,
                'chat_logs', 0,
                'admission_reservations', 0,
                'privacy_operations', 0,
                'profiles', 0
            )
        );
    END IF;

    -- Same per-user transaction lock as commit_turn: a concurrent turn
    -- commit of this user can never interleave with the deletion.
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(v_job.user_id, 0)
    );

    DELETE FROM public.outbox_events WHERE user_id = v_job.user_id;
    GET DIAGNOSTICS v_count_outbox = ROW_COUNT;

    DELETE FROM public.turn_requests WHERE user_id = v_job.user_id;
    GET DIAGNOSTICS v_count_turn_requests = ROW_COUNT;

    DELETE FROM public.archival_extractions WHERE user_id = v_job.user_id;
    GET DIAGNOSTICS v_count_archival = ROW_COUNT;

    DELETE FROM public.memories WHERE user_id = v_job.user_id;
    GET DIAGNOSTICS v_count_memories = ROW_COUNT;

    DELETE FROM public.chat_logs WHERE user_id = v_job.user_id;
    GET DIAGNOSTICS v_count_chat_logs = ROW_COUNT;

    DELETE FROM public.admission_reservations WHERE user_id = v_job.user_id;
    GET DIAGNOSTICS v_count_admission = ROW_COUNT;

    DELETE FROM public.privacy_operations WHERE user_id = v_job.user_id;
    GET DIAGNOSTICS v_count_privacy = ROW_COUNT;

    DELETE FROM public.profiles WHERE user_id = v_job.user_id;
    GET DIAGNOSTICS v_count_profiles = ROW_COUNT;

    -- Commit invariant: db_purged_at becomes non-NULL in the SAME
    -- transaction that completed the purge. The job stays (tombstone).
    UPDATE public.account_deletion_jobs
    SET db_purged_at = v_now,
        updated_at = v_now
    WHERE job_id = v_job.job_id;

    RETURN jsonb_build_object(
        'status', 'purged',
        'job_id', v_job.job_id::text,
        'db_purged_at', v_now,
        'counts', jsonb_build_object(
            'outbox_events', v_count_outbox,
            'turn_requests', v_count_turn_requests,
            'archival_extractions', v_count_archival,
            'memories', v_count_memories,
            'chat_logs', v_count_chat_logs,
            'admission_reservations', v_count_admission,
            'privacy_operations', v_count_privacy,
            'profiles', v_count_profiles
        )
    );
EXCEPTION
    WHEN OTHERS THEN
        RAISE EXCEPTION 'persistence error' USING ERRCODE = 'P0001';
END;
$$;

REVOKE ALL ON FUNCTION public.account_deletion_purge(uuid, text, text)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.account_deletion_purge(uuid, text, text)
    TO service_role;

-- =================================================================
-- 9. account_deletion_record_failure
-- =================================================================
-- A worker reports a sanitized failure: the job moves to failed with the
-- error_code, its lease is released and next_attempt_at is set to a
-- backoff derived from attempts (30s * attempts, capped at 1h) so the
-- job becomes retry-eligible later. The raw user_id stays (retry still
-- needs it).
CREATE OR REPLACE FUNCTION public.account_deletion_record_failure(
    p_job_id uuid,
    p_worker_id text,
    p_error_code text
) RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_job public.account_deletion_jobs;
    v_now timestamptz := clock_timestamp();
    v_next_attempt_at timestamptz;
BEGIN
    IF p_error_code IS NULL OR p_error_code !~ '^[a-z0-9_]{1,64}$' THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'validation_failed',
                'message', 'error_code is invalid'
            )
        );
    END IF;

    v_job := public.account_deletion_assert_owner(p_job_id, p_worker_id);

    v_next_attempt_at := v_now
        + make_interval(secs => LEAST(3600, 30 * v_job.attempts)::double precision);

    UPDATE public.account_deletion_jobs
    SET status = 'failed',
        error_code = p_error_code,
        lease_owner = NULL,
        lease_expires_at = NULL,
        next_attempt_at = v_next_attempt_at,
        updated_at = v_now
    WHERE job_id = v_job.job_id;

    RETURN jsonb_build_object(
        'status', 'failed',
        'job_id', v_job.job_id::text,
        'error_code', p_error_code,
        'next_attempt_at', v_next_attempt_at
    );
EXCEPTION
    WHEN OTHERS THEN
        RAISE EXCEPTION 'persistence error' USING ERRCODE = 'P0001';
END;
$$;

REVOKE ALL ON FUNCTION public.account_deletion_record_failure(uuid, text, text)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.account_deletion_record_failure(uuid, text, text)
    TO service_role;

-- =================================================================
-- 10. account_deletion_record_retry
-- =================================================================
-- A worker voluntarily defers the job (e.g. transient infrastructure
-- unavailability): the job returns to pending with a backoff next_attempt
-- and its lease released. Distinct from a failure (status stays failed
-- there; here the job becomes pending again, error_code cleared).
CREATE OR REPLACE FUNCTION public.account_deletion_record_retry(
    p_job_id uuid,
    p_worker_id text
) RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_job public.account_deletion_jobs;
    v_now timestamptz := clock_timestamp();
    v_next_attempt_at timestamptz;
BEGIN
    v_job := public.account_deletion_assert_owner(p_job_id, p_worker_id);

    v_next_attempt_at := v_now
        + make_interval(secs => LEAST(3600, 30 * v_job.attempts)::double precision);

    UPDATE public.account_deletion_jobs
    SET status = 'pending',
        error_code = NULL,
        lease_owner = NULL,
        lease_expires_at = NULL,
        next_attempt_at = v_next_attempt_at,
        updated_at = v_now
    WHERE job_id = v_job.job_id;

    RETURN jsonb_build_object(
        'status', 'retry_scheduled',
        'job_id', v_job.job_id::text,
        'next_attempt_at', v_next_attempt_at
    );
EXCEPTION
    WHEN OTHERS THEN
        RAISE EXCEPTION 'persistence error' USING ERRCODE = 'P0001';
END;
$$;

REVOKE ALL ON FUNCTION public.account_deletion_record_retry(uuid, text)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.account_deletion_record_retry(uuid, text)
    TO service_role;

-- =================================================================
-- 11. account_deletion_finalize
-- =================================================================
-- Called by the future worker AFTER it confirmed the Auth deletion
-- (#325). Requires the DB purge to be committed (db_purged_at set) and
-- current lease ownership. Marks the job completed and MINIMIZES
-- identity: user_id -> NULL (only the server-derived HMAC reference
-- remains for bounded audit/idempotency).
CREATE OR REPLACE FUNCTION public.account_deletion_finalize(
    p_job_id uuid,
    p_worker_id text
) RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_job public.account_deletion_jobs;
    v_now timestamptz := clock_timestamp();
BEGIN
    v_job := public.account_deletion_assert_owner(p_job_id, p_worker_id);

    IF v_job.db_purged_at IS NULL THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'state_conflict',
                'message', 'cannot finalize before the database purge is committed'
            )
        );
    END IF;

    UPDATE public.account_deletion_jobs
    SET status = 'completed',
        user_id = NULL,
        lease_owner = NULL,
        lease_expires_at = NULL,
        completed_at = v_now,
        updated_at = v_now
    WHERE job_id = v_job.job_id;

    RETURN jsonb_build_object(
        'status', 'completed',
        'job_id', v_job.job_id::text,
        'completed_at', v_now,
        'db_purged_at', v_job.db_purged_at
    );
EXCEPTION
    WHEN OTHERS THEN
        RAISE EXCEPTION 'persistence error' USING ERRCODE = 'P0001';
END;
$$;

REVOKE ALL ON FUNCTION public.account_deletion_finalize(uuid, text)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.account_deletion_finalize(uuid, text)
    TO service_role;

-- =================================================================
-- 12. account_deletion_purge_completed (retention of tombstones)
-- =================================================================
-- Deletes up to p_batch_size COMPLETED jobs whose completed_at is older
-- than the effective cutoff. The cutoff is clamped to never exceed
-- clock_timestamp() - 30 days (authoritative PostgreSQL time), reusing
-- the #316 model: a fast process clock can only reduce progress, never
-- advance deletion. pending/processing/failed jobs are never removed by
-- age. Idempotent and bounded.
CREATE OR REPLACE FUNCTION public.account_deletion_purge_completed(
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
    IF p_cutoff IS NULL OR p_batch_size IS NULL
       OR p_batch_size NOT BETWEEN 1 AND 1000 THEN
        RAISE EXCEPTION 'invalid retention parameters';
    END IF;

    v_cutoff := LEAST(p_cutoff, clock_timestamp() - interval '30 days');

    DELETE FROM public.account_deletion_jobs
    WHERE job_id IN (
        SELECT job_id
        FROM public.account_deletion_jobs
        WHERE status = 'completed'
          AND completed_at < v_cutoff
        ORDER BY completed_at
        LIMIT p_batch_size
    );
    GET DIAGNOSTICS v_deleted = ROW_COUNT;
    RETURN v_deleted;
END;
$$;

REVOKE ALL ON FUNCTION public.account_deletion_purge_completed(timestamptz, integer)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.account_deletion_purge_completed(timestamptz, integer)
    TO service_role;

-- =================================================================
-- 13. Documentation comments
-- =================================================================
COMMENT ON TABLE public.account_deletion_jobs IS
'Durable, server-owned account deletion ledger (#324). Tombstone survives the profiles DELETE; no FK to profiles. States: pending/processing/completed/failed. Raw user_id is minimized to NULL on completion; the domain-separated HMAC reference persists for bounded audit/idempotency. RLS + FORCE RLS, zero policies, no table grants (not even service_role).';

COMMENT ON FUNCTION public.account_deletion_request(text, text, uuid, text) IS
'Register or replay an account deletion request (#324). Same (ref, operation_id) with same fingerprint replays; divergent fingerprint conflicts sanitized. SECURITY DEFINER, service_role only.';

COMMENT ON FUNCTION public.account_deletion_has_tombstone(text) IS
'Sanitized tombstone lookup by server-derived reference (#324/#326). SECURITY DEFINER, service_role only.';

COMMENT ON FUNCTION public.account_deletion_acquire_lease(text, integer, integer) IS
'Claim one eligible account deletion job with FOR UPDATE SKIP LOCKED; increments attempts (#324). SECURITY DEFINER, service_role only.';

COMMENT ON FUNCTION public.account_deletion_purge(uuid, text, text) IS
'Atomic per-user PostgreSQL purge in one transaction with the commit_turn advisory lock; db_purged_at set in the same transaction; replay-safe; tombstone never deleted (#324). SECURITY DEFINER, service_role only.';

COMMENT ON FUNCTION public.account_deletion_record_failure(uuid, text, text) IS
'Record a sanitized failure, release the lease, schedule a backoff retry (#324). SECURITY DEFINER, service_role only.';

COMMENT ON FUNCTION public.account_deletion_record_retry(uuid, text) IS
'Voluntarily defer a job: back to pending with backoff, lease released (#324). SECURITY DEFINER, service_role only.';

COMMENT ON FUNCTION public.account_deletion_finalize(uuid, text) IS
'Complete a job after the future worker confirms the Auth deletion (#325); requires db_purged_at; minimizes user_id to NULL (#324). SECURITY DEFINER, service_role only.';

COMMENT ON FUNCTION public.account_deletion_purge_completed(timestamptz, integer) IS
'Retention of completed tombstones older than 30 days; cutoff clamped by the DB; active/failed jobs never aged out (#324, model of #316). SECURITY DEFINER, service_role only.';
