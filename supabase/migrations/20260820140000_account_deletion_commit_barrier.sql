-- ============================================================================
-- Account deletion commit barrier (#329).
-- ============================================================================
-- Closes the TOCTOU window between the #326 HTTP tombstone preflight and the
-- transactional turn commit: a /chat request that passed the HTTP gate
-- BEFORE an account deletion request was accepted can still reach the
-- commit_turn boundary afterwards. The HTTP gate is fail-fast only; the
-- authoritative invariant lives at the transactional boundary, under the
-- SAME per-user advisory lock used by account_deletion_request and the
-- purge (pg_advisory_xact_lock(hashtextextended(user_id, 0))).
--
-- Shape
-- =====
--   1. account_deletion_commit_barrier(p_user_ref_hmac_sha256)
--      A read-only helper (no lock needed: it runs INSIDE the caller's
--      transaction while the caller already holds the per-user advisory
--      xact lock, so the check and the writes execute as one serialized
--      unit). The lookup is by the server-derived HMAC reference ONLY, so
--      it remains authoritative AFTER finalize (when user_id is minimized
--      to NULL).
--   2. commit_turn gains an OPTIONAL p_account_deletion_user_ref text
--      parameter (DEFAULT NULL). The validation, the writes and the
--      failure semantics are byte-identical to the historical contract;
--      existing callers pass nothing and behave exactly as before (no new
--      dependency, no runtime cost for unrelated users, no migration
--      history rewritten). When present, the barrier runs immediately
--      after the advisory lock is acquired and BEFORE any write, and a
--      blocking tombstone short-circuits the commit with a sanitized
--      account_deletion_pending conflict envelope: no profile is created
--      or updated, no chat_logs, turn_requests or outbox_events are
--      written.
--
-- Post-finalize correctness
-- =========================
-- The ledger keeps reporting the tombstone through retention: after
-- purge/finalize the job stays in account_deletion_jobs with user_id NULL,
-- keyed solely by user_ref_hmac_sha256. The barrier query uses the same
-- identity column, so a commit attempt after purge, finalize or any later
-- stage is blocked identically to a commit attempt during
-- pending/processing/failed. There is no bypass through minimization.
--
-- Fail-closed
-- ===========
--   * An invalid (NULL, malformed, or non-hex) reference never reads as
--     "user active": a server bug in the derivation surfaces as the
--     standard sanitized persistence error and rolls the whole commit
--     transaction back.
--   * Any unexpected persistence failure inside the barrier propagates as
--     the standard sanitized persistence error (same rollback semantics).
--   * No tombstone -> the commit proceeds exactly as before.

-- =================================================================
-- 0. PREFLIGHT: fail closed on missing dependencies
-- =================================================================
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
          AND p.proname = 'account_deletion_has_tombstone'
    ) THEN
        RAISE EXCEPTION 'Cannot apply account deletion commit barrier: account_deletion_has_tombstone is missing (unexpected drift)'
            USING ERRCODE = '23514';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relname = 'account_deletion_jobs'
    ) THEN
        RAISE EXCEPTION 'Cannot apply account deletion commit barrier: account_deletion_jobs is missing (unexpected drift)'
            USING ERRCODE = '23514';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
          AND p.proname = 'commit_turn'
    ) THEN
        RAISE EXCEPTION 'Cannot apply account deletion commit barrier: commit_turn is missing (unexpected drift)'
            USING ERRCODE = '23514';
    END IF;
END;
$$;

-- =================================================================
-- 1. account_deletion_commit_barrier
-- =================================================================
-- Read-only barrier check. Must be called while the caller already holds
-- the same per-user advisory xact lock as account_deletion_request (which
-- commit_turn acquires before any write). Reports whether a blocking
-- tombstone exists for the server-derived reference. The lookup column is
-- the persistent HMAC reference, NOT the raw user_id, so the check stays
-- authoritative after finalize minimizes user_id to NULL.
CREATE OR REPLACE FUNCTION public.account_deletion_commit_barrier(
    p_user_ref_hmac_sha256 text
) RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_exists boolean;
BEGIN
    IF p_user_ref_hmac_sha256 IS NULL
       OR p_user_ref_hmac_sha256 !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'persistence error' USING ERRCODE = 'P0001';
    END IF;

    SELECT EXISTS (
        SELECT 1
        FROM public.account_deletion_jobs
        WHERE user_ref_hmac_sha256 = p_user_ref_hmac_sha256
    ) INTO v_exists;

    IF v_exists THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'account_deletion_pending',
                'message', 'Account deletion is pending.'
            )
        );
    END IF;
    RETURN jsonb_build_object('blocked', false);
EXCEPTION
    WHEN OTHERS THEN
        -- Fail closed: every barrier anomaly surfaces as the standard
        -- sanitized persistence error, rolling the caller's transaction
        -- back (no writes ever happen).
        RAISE EXCEPTION 'persistence error' USING ERRCODE = 'P0001';
END;
$$;

REVOKE ALL ON FUNCTION public.account_deletion_commit_barrier(text)
    FROM PUBLIC, anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.account_deletion_commit_barrier(text)
    TO service_role;

-- =================================================================
-- 2. commit_turn: optional commit-side deletion barrier
-- =================================================================
-- The historical commit_turn (12 parameters, migration 05) is REPLACED by
-- a single 13-parameter signature with an optional
-- p_account_deletion_user_ref text DEFAULT NULL. One signature ONLY: an
-- overload (12 + 13 params) makes every positional call ambiguous
-- (`function ... is not unique`), because PostgreSQL cannot prefer one
-- default-trailing signature over the other — even explicit casts on the
-- first 11/12 arguments leave both candidates equally good. Keeping a
-- single signature also means existing callers (including the #326 worker)
-- invoke the same function, and the existing grants simply move to the
-- new signature.
--
-- The function body, validation, write order, CAS, reclaim and failure
-- semantics are copied byte-for-byte from the historical contract so
-- existing callers keep identical behavior.
--
-- When the derivation parameter is provided, the barrier runs inside the
-- SAME transaction, immediately after the per-user advisory lock is
-- acquired (the same lock as account_deletion_request / the purge) and
-- BEFORE any write path. A blocking tombstone short-circuits the commit
-- with a sanitized account_deletion_pending conflict envelope: no profile
-- is created or updated, no chat_logs, turn_requests or outbox_events are
-- written, and the transaction ends via the normal RETURN path (no
-- exception), keeping the commit contract unchanged.
DROP FUNCTION IF EXISTS public.commit_turn(
    text, uuid, bigint, text, text, text, jsonb, jsonb, text, jsonb,
    jsonb, text
);
CREATE OR REPLACE FUNCTION public.commit_turn(
    p_authenticated_user_id text,
    p_request_id uuid,
    p_expected_revision bigint,
    p_user_message text,
    p_assistant_message text,
    p_payload_hash_sha256 text,
    p_emotional_state jsonb,
    p_relationship_state jsonb,
    p_public_response text,
    p_replay_payload jsonb,
    p_outbox_events jsonb DEFAULT '[]'::jsonb,
    p_lease_owner text DEFAULT NULL,
    p_account_deletion_user_ref text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    -- Profile state
    v_profile_exists boolean;
    v_current_revision bigint;
    v_new_revision bigint;
    v_profile_user_id text;

    -- Request state
    v_request_exists boolean;
    v_existing_payload_hash text;
    v_existing_status text;
    v_existing_committed_revision bigint;
    v_existing_replay_payload jsonb;
    v_existing_created_at timestamptz;
    v_existing_completed_at timestamptz;
    v_existing_lease_owner text;
    v_existing_lease_expires_at timestamptz;
    v_turn_request_id uuid;
    v_existing_turn_request_id uuid;
    v_reclaim_updated uuid;

    -- Message IDs
    v_user_message_id bigint;
    v_assistant_message_id bigint;

    -- Result building
    v_result jsonb;

    -- Timestamp
    v_now timestamptz := timezone('utc'::text, now());

    -- Loop counters
    v_i integer;
    v_outbox_count integer;

    -- Temporary variables for outbox processing
    v_event_obj jsonb;
    v_event_type text;
    v_event_payload jsonb;
    v_event_idempotency_key text;

    -- Replay payload forbidden keys (prompts / internal instructions)
    v_replay_forbidden text[] := ARRAY[
        'prompt', 'system_prompt', 'meta_cognition',
        'internal_instructions', 'message', 'user_message',
        'assistant_message', 'content'
    ];
BEGIN
    -- =============================================================
    -- Step 0: Input validation (fail fast, stable envelopes)
    -- =============================================================

    IF p_authenticated_user_id IS NULL OR p_authenticated_user_id = '' THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'validation_failed',
                'message', 'authenticated_user_id is required'
            )
        );
    END IF;
    IF p_request_id IS NULL THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'validation_failed',
                'message', 'request_id is required'
            )
        );
    END IF;
    IF p_expected_revision IS NULL OR p_expected_revision < 0 THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'validation_failed',
                'message', 'expected_revision must be non-negative'
            )
        );
    END IF;
    IF p_user_message IS NULL THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'validation_failed',
                'message', 'user_message is required'
            )
        );
    END IF;
    IF p_assistant_message IS NULL THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'validation_failed',
                'message', 'assistant_message is required'
            )
        );
    END IF;
    IF p_payload_hash_sha256 IS NULL OR NOT (p_payload_hash_sha256 ~ '^[0-9a-f]{64}$') THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'validation_failed',
                'message', 'payload_hash_sha256 must be a 64-character hex string'
            )
        );
    END IF;
    -- replay_payload is the authoritative public result. It MUST be a JSON
    -- object containing response (== p_public_response) and message_id (the
    -- public identifier returned as assistant_message_id).
    IF p_replay_payload IS NULL OR jsonb_typeof(p_replay_payload) <> 'object' THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'validation_failed',
                'message', 'replay_payload must be a JSON object'
            )
        );
    END IF;
    IF NOT public.jsonb_keys_subset_of(
        p_replay_payload,
        ARRAY['response', 'emotion_state', 'message_id', 'request_id', 'duration_ms']
    ) THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'validation_failed',
                'message', 'replay_payload has invalid keys'
            )
        );
    END IF;
    IF public.jsonb_has_forbidden_key(p_replay_payload, v_replay_forbidden) THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'validation_failed',
                'message', 'replay_payload contains forbidden keys'
            )
        );
    END IF;
    IF jsonb_typeof(p_replay_payload->'response') <> 'string'
       OR (p_replay_payload->>'response') IS NULL THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'validation_failed',
                'message', 'replay_payload must contain response'
            )
        );
    END IF;
    IF p_replay_payload ? 'request_id'
       AND (
           jsonb_typeof(p_replay_payload->'request_id') <> 'string'
           OR (p_replay_payload->>'request_id') !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR (p_replay_payload->>'request_id') <> p_request_id::text
       ) THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'validation_failed',
                'message', 'replay_payload.request_id must equal request_id'
            )
        );
    END IF;
    -- Single authoritative source for the public response: p_public_response
    -- must be byte-equal to the persisted replay_payload.response.
    IF p_public_response IS NULL OR p_public_response <> (p_replay_payload->>'response') THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'validation_failed',
                'message', 'public_response must equal replay_payload.response'
            )
        );
    END IF;
    IF (p_replay_payload->>'message_id') IS NULL
       OR NOT ((p_replay_payload->>'message_id') ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'validation_failed',
                'message', 'replay_payload.message_id must be a valid UUID'
            )
        );
    END IF;
    IF octet_length(p_replay_payload::text) > 8192 THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'validation_failed',
                'message', 'replay_payload exceeds 8192 bytes'
            )
        );
    END IF;
    -- Snapshot validation (object with schema_version=1, no identity/internal
    -- keys, fundamental structure present, size-bounded).
    IF NOT public.jsonb_snapshot_contract(p_emotional_state, 'emotional') THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'validation_failed',
                'message', 'emotional_state violates the snapshot contract'
            )
        );
    END IF;
    IF NOT public.jsonb_snapshot_contract(p_relationship_state, 'relationship') THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'validation_failed',
                'message', 'relationship_state violates the snapshot contract'
            )
        );
    END IF;
    IF p_outbox_events IS NULL OR jsonb_typeof(p_outbox_events) <> 'array' THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'validation_failed',
                'message', 'outbox_events must be a JSON array'
            )
        );
    END IF;
    -- Lease owner, when provided, must satisfy the sanitized identifier regex.
    IF p_lease_owner IS NOT NULL
       AND NOT (p_lease_owner ~ '^[A-Za-z0-9_.:-]{1,64}$') THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'validation_failed',
                'message', 'lease_owner is invalid'
            )
        );
    END IF;
    -- Account deletion barrier reference, when provided, must be the exact
    -- server-derived HMAC format (never the raw user_id).
    IF p_account_deletion_user_ref IS NOT NULL
       AND NOT (p_account_deletion_user_ref ~ '^[0-9a-f]{64}$') THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'validation_failed',
                'message', 'account_deletion_user_ref must be 64 lowercase hex characters'
            )
        );
    END IF;
    -- Outbox events validation (shape, types, keys, value contract).
    v_outbox_count := jsonb_array_length(p_outbox_events);
    FOR v_i IN 1..v_outbox_count LOOP
        v_event_obj := p_outbox_events->(v_i-1);
        IF v_event_obj IS NULL OR jsonb_typeof(v_event_obj) <> 'object' THEN
            RETURN jsonb_build_object(
                'error', jsonb_build_object(
                    'code', 'validation_failed',
                    'message', 'outbox event must be a JSON object'
                )
            );
        END IF;
        v_event_type := v_event_obj->>'event_type';
        v_event_payload := v_event_obj->'payload';
        v_event_idempotency_key := v_event_obj->>'idempotency_key';
        IF v_event_type IS NULL OR NOT (v_event_type ~ '^[a-z0-9_]{1,64}$') THEN
            RETURN jsonb_build_object(
                'error', jsonb_build_object(
                    'code', 'validation_failed',
                    'message', 'outbox event_type is invalid'
                )
            );
        END IF;
        IF v_event_payload IS NULL OR jsonb_typeof(v_event_payload) <> 'object' THEN
            RETURN jsonb_build_object(
                'error', jsonb_build_object(
                    'code', 'validation_failed',
                    'message', 'outbox event payload must be a JSON object'
                )
            );
        END IF;
        IF NOT public.jsonb_keys_subset_of(
            v_event_payload,
            ARRAY['ref', 'request_id', 'turn_id', 'message_id', 'entity_id', 'kind', 'version']
        ) THEN
            RETURN jsonb_build_object(
                'error', jsonb_build_object(
                    'code', 'validation_failed',
                    'message', 'outbox event payload has invalid keys'
                )
            );
        END IF;
        IF public.jsonb_has_forbidden_key(v_event_payload, v_replay_forbidden) THEN
            RETURN jsonb_build_object(
                'error', jsonb_build_object(
                    'code', 'validation_failed',
                    'message', 'outbox event payload contains forbidden keys'
                )
            );
        END IF;
        IF NOT public.jsonb_outbox_payload_value_contract(v_event_payload) THEN
            RETURN jsonb_build_object(
                'error', jsonb_build_object(
                    'code', 'validation_failed',
                    'message', 'outbox event payload violates the value contract'
                )
            );
        END IF;
        IF v_event_idempotency_key IS NULL
           OR NOT (v_event_idempotency_key ~ '^[A-Za-z0-9_.:-]{1,128}$') THEN
            RETURN jsonb_build_object(
                'error', jsonb_build_object(
                    'code', 'validation_failed',
                    'message', 'outbox idempotency_key is invalid'
                )
            );
        END IF;
    END LOOP;

    -- =============================================================
    -- Step 1: Acquire per-user lock (64-bit advisory key)
    -- =============================================================
    -- hashtextextended returns a bigint (64-bit) key, avoiding the 32-bit
    -- hashtext collision space. Different users (almost surely) map to
    -- different keys, so distinct users are never globally serialized.
    PERFORM pg_advisory_xact_lock(hashtextextended(p_authenticated_user_id, 0));

    -- =============================================================
    -- Step 1b: Account deletion commit barrier (optional, fail-closed)
    -- =============================================================
    -- Authoritative, race-safe boundary: the advisory lock acquired above
    -- is the SAME per-user lock held by account_deletion_request and the
    -- deletion purge, so the tombstone check and the writes below execute
    -- as one serialized unit under the lock. The lookup uses the
    -- server-derived HMAC reference only (never the raw user_id), so the
    -- check remains authoritative after finalize minimizes user_id to
    -- NULL. A blocking tombstone short-circuits the commit BEFORE any
    -- write: no profile, chat_logs, turn_requests or outbox_events rows
    -- are created or modified, and no exception is raised (the normal
    -- RETURN path ends the transaction cleanly).
    IF p_account_deletion_user_ref IS NOT NULL THEN
        BEGIN
            SELECT public.account_deletion_commit_barrier(p_account_deletion_user_ref)
            INTO v_result;
        EXCEPTION
            WHEN OTHERS THEN
                -- Fail closed: any barrier anomaly rolls the whole
                -- transaction back (no writes ever happen).
                RAISE EXCEPTION 'persistence error' USING ERRCODE = 'P0001';
        END;
        IF v_result ? 'error'
           AND (v_result->'error'->>'code') = 'account_deletion_pending' THEN
            RETURN jsonb_build_object(
                'error', jsonb_build_object(
                    'code', 'account_deletion_pending',
                    'message', 'Account deletion is pending.'
                )
            );
        END IF;
    END IF;

    -- =============================================================
    -- Step 2: Check existing profile and get current revision
    -- =============================================================
    SELECT user_id, revision INTO v_profile_user_id, v_current_revision
    FROM public.profiles
    WHERE user_id = p_authenticated_user_id
    FOR UPDATE;

    v_profile_exists := (v_profile_user_id IS NOT NULL);

    IF NOT v_profile_exists THEN
        v_current_revision := 0;
    END IF;

    -- =============================================================
    -- Step 3: Check for existing request with same (user_id, request_id)
    -- MUST run BEFORE CAS so exact replays and lease reclaims work
    -- =============================================================
    SELECT EXISTS (
        SELECT 1 FROM public.turn_requests
        WHERE user_id = p_authenticated_user_id AND request_id = p_request_id
    ) INTO v_request_exists;

    IF v_request_exists THEN
        SELECT
            id,
            payload_hash_sha256,
            status,
            committed_revision,
            replay_payload,
            created_at,
            completed_at,
            lease_owner,
            lease_expires_at
        INTO
            v_existing_turn_request_id,
            v_existing_payload_hash,
            v_existing_status,
            v_existing_committed_revision,
            v_existing_replay_payload,
            v_existing_created_at,
            v_existing_completed_at,
            v_existing_lease_owner,
            v_existing_lease_expires_at
        FROM public.turn_requests
        WHERE user_id = p_authenticated_user_id AND request_id = p_request_id;

        -- Any status + different hash: ALWAYS a payload conflict. Reclaim is
        -- only ever allowed for the SAME hash.
        IF v_existing_payload_hash <> p_payload_hash_sha256 THEN
            RETURN jsonb_build_object(
                'error', jsonb_build_object(
                    'code', 'request_payload_conflict',
                    'message', 'Request ID already exists with a different payload hash',
                    'expected_revision', p_expected_revision,
                    'request_id', p_request_id::text
                )
            );
        END IF;

        -- completed + same hash: idempotent replay WITHOUT any writes.
        IF v_existing_status = 'completed' THEN
            RETURN public.commit_turn_build_result(p_authenticated_user_id, p_request_id);
        END IF;

        -- pending/expired + same hash: lease / reclaim semantics.
        IF v_existing_status = 'pending'
           AND v_existing_lease_expires_at IS NOT NULL
           AND v_existing_lease_expires_at > v_now
           AND (p_lease_owner IS NULL OR v_existing_lease_owner <> p_lease_owner) THEN
            -- Active lease owned by another worker: stable result.
            RETURN jsonb_build_object(
                'error', jsonb_build_object(
                    'code', 'request_in_progress',
                    'message', 'Request is already in progress by another worker',
                    'expected_revision', p_expected_revision,
                    'request_id', p_request_id::text
                )
            );
        END IF;

        -- Reclaim (expired lease or expired request) requires a lease owner.
        IF p_lease_owner IS NULL THEN
            RETURN jsonb_build_object(
                'error', jsonb_build_object(
                    'code', 'lease_conflict',
                    'message', 'lease_owner is required to claim or reclaim a request',
                    'expected_revision', p_expected_revision,
                    'request_id', p_request_id::text
                )
            );
        END IF;

        -- Proceed: the atomic conditional UPDATE below is the reclaim.
        v_turn_request_id := v_existing_turn_request_id;
    END IF;

    -- =============================================================
    -- Step 4: Validate CAS: expected_revision must match current revision
    -- Only applies to write paths (never to the completed replay above).
    -- =============================================================
    IF v_current_revision <> p_expected_revision THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'revision_mismatch',
                'message', 'Profile revision does not match the expected value',
                'expected_revision', p_expected_revision,
                'actual_revision', v_current_revision
            )
        );
    END IF;

    -- =============================================================
    -- Step 5: Update or create profile BEFORE inserting messages
    -- (chat_logs.user_id references profiles, so the profile must
    -- exist before any message row is written). Snapshots are applied
    -- and revision is incremented exactly once here.
    -- =============================================================
    v_new_revision := v_current_revision + 1;

    IF v_profile_exists THEN
        UPDATE public.profiles
        SET
            emotional_state = COALESCE(p_emotional_state, emotional_state),
            relationship_state = COALESCE(p_relationship_state, relationship_state),
            revision = v_new_revision,
            updated_at = v_now
        WHERE user_id = p_authenticated_user_id;
    ELSE
        IF p_emotional_state IS NULL OR p_relationship_state IS NULL THEN
            RETURN jsonb_build_object(
                'error', jsonb_build_object(
                    'code', 'validation_failed',
                    'message', 'new profiles require complete v1 snapshots'
                )
            );
        END IF;
        INSERT INTO public.profiles (
            user_id, persona_config, user_profile, relationship_state, emotional_state,
            revision, updated_at
        ) VALUES (
            p_authenticated_user_id, NULL, NULL,
            p_relationship_state,
            p_emotional_state,
            v_new_revision, v_now
        );
    END IF;

    -- =============================================================
    -- Step 6: Insert user message into chat_logs
    -- =============================================================
    INSERT INTO public.chat_logs (user_id, role, content, created_at)
    VALUES (p_authenticated_user_id, 'user', p_user_message, v_now)
    RETURNING id INTO v_user_message_id;

    -- =============================================================
    -- Step 7: Insert assistant message into chat_logs
    -- =============================================================
    INSERT INTO public.chat_logs (user_id, role, content, created_at)
    VALUES (p_authenticated_user_id, 'assistant', p_assistant_message, v_now)
    RETURNING id INTO v_assistant_message_id;

    -- =============================================================
    -- Step 8: Complete turn_requests
    --   - New request: INSERT (status completed)
    --   - pending/expired with same hash: ATOMIC conditional reclaim via
    --     UPDATE ... WHERE ... RETURNING confirming id, user_id, request_id,
    --     payload hash, expected status, lease ownership and lease expiry.
    -- =============================================================
    IF v_turn_request_id IS NOT NULL THEN
        UPDATE public.turn_requests tr
        SET
            status = 'completed',
            committed_revision = v_new_revision,
            user_message_chat_log_id = v_user_message_id,
            assistant_message_chat_log_id = v_assistant_message_id,
            replay_payload = p_replay_payload,
            updated_at = v_now,
            completed_at = v_now,
            error_code = NULL,
            lease_owner = NULL,
            lease_expires_at = NULL
        WHERE tr.id = v_turn_request_id
          AND tr.user_id = p_authenticated_user_id
          AND tr.request_id = p_request_id
          AND tr.payload_hash_sha256 = p_payload_hash_sha256
          AND tr.status IN ('pending', 'expired')
          AND (
                -- pending with ACTIVE lease of the same worker: continue
                (tr.status = 'pending'
                 AND tr.lease_expires_at > v_now
                 AND tr.lease_owner = p_lease_owner)
                OR
                -- pending with EXPIRED lease: reclaim
                (tr.status = 'pending' AND tr.lease_expires_at <= v_now)
                OR
                -- expired request (no lease): reclaim
                (tr.status = 'expired')
          )
        RETURNING tr.id INTO v_reclaim_updated;

        IF v_reclaim_updated IS NULL THEN
            -- The row changed between the SELECT and the UPDATE (or the lease
            -- was re-taken): controlled conflict, never a payload rewrite.
            RAISE EXCEPTION 'lease conflict' USING ERRCODE = 'P0002';
        END IF;
    ELSE
        INSERT INTO public.turn_requests (
            user_id, request_id, payload_hash_sha256, status,
            expected_revision, committed_revision,
            user_message_chat_log_id, assistant_message_chat_log_id,
            replay_payload, created_at, updated_at, completed_at
        ) VALUES (
            p_authenticated_user_id, p_request_id, p_payload_hash_sha256, 'completed',
            p_expected_revision, v_new_revision,
            v_user_message_id, v_assistant_message_id,
            p_replay_payload, v_now, v_now, v_now
        ) RETURNING id INTO v_turn_request_id;
    END IF;

    -- =============================================================
    -- Step 9: Insert outbox events (idempotent, FK to turn_request id)
    -- =============================================================
    v_outbox_count := jsonb_array_length(p_outbox_events);

    FOR v_i IN 1..v_outbox_count LOOP
        v_event_obj := p_outbox_events->(v_i-1);
        v_event_type := v_event_obj->>'event_type';
        v_event_payload := v_event_obj->'payload';
        v_event_idempotency_key := v_event_obj->>'idempotency_key';

        -- (Validated in Step 0; repeated defensively before insert.)
        INSERT INTO public.outbox_events (
            event_type, user_id, turn_request_id, payload, status,
            attempts, next_attempt_at, idempotency_key,
            contract_version, created_at, updated_at
        ) VALUES (
            v_event_type, p_authenticated_user_id, v_turn_request_id,
            v_event_payload, 'pending',
            0, v_now + INTERVAL '1 second', v_event_idempotency_key,
            1, v_now, v_now
        );
    END LOOP;

    SELECT COALESCE(
        jsonb_agg(
            jsonb_build_object(
                'id', e.id::text,
                'event_type', e.event_type,
                'idempotency_key', e.idempotency_key,
                'turn_request_id', e.turn_request_id::text,
                'contract_version', e.contract_version
            ) ORDER BY e.id
        ), '[]'::jsonb
    ) INTO v_result
    FROM public.outbox_events e
    WHERE e.user_id = p_authenticated_user_id AND e.turn_request_id = v_turn_request_id;

    UPDATE public.turn_requests
    SET replay_outbox_refs = v_result
    WHERE id = v_turn_request_id;

    -- =============================================================
    -- Step 10: Build and return the public result from PERSISTED rows
    -- (identical to the replay path).
    -- =============================================================
    RETURN public.commit_turn_build_result(p_authenticated_user_id, p_request_id);

EXCEPTION
    WHEN SQLSTATE 'P0002' THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'lease_conflict',
                'message', 'Request state changed; reclaim failed',
                'expected_revision', p_expected_revision,
                'request_id', p_request_id::text
            )
        );
    WHEN OTHERS THEN
        -- Unexpected PostgreSQL failure. Never expose SQLERRM, SQLSTATE,
        -- constraint names or payload; propagate a constant sanitized message
        -- so the RPC fails (rollback is automatic) and the Python layer maps
        -- it to PersistenceError.
        RAISE EXCEPTION 'persistence error' USING ERRCODE = 'P0001';
END;
$$;

REVOKE ALL ON FUNCTION public.commit_turn(
    text, uuid, bigint, text, text, text, jsonb, jsonb, text, jsonb, jsonb, text, text
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.commit_turn(
    text, uuid, bigint, text, text, text, jsonb, jsonb, text, jsonb, jsonb, text, text
) TO service_role;

-- =================================================================
-- 3. Restore the public contract comment (lost when the historical
--    12-parameter signature was dropped above).
-- =================================================================
COMMENT ON FUNCTION public.commit_turn(
    text, uuid, bigint, text, text, text, jsonb, jsonb, text, jsonb, jsonb, text, text
) IS 'Atomic turn commit RPC (#271). Executes a complete turn as a single transaction: profile CAS, message inserts, snapshot update, turn_request completion, outbox events. Fail-closed: any error rolls back everything. Replay-safe: idempotent retries return the exact stored public result without writes. Lease/reclaim is protected by an atomic conditional UPDATE ... RETURNING. When p_account_deletion_user_ref is provided (server-derived HMAC, hex-64), the account deletion commit barrier runs inside the same transaction under the same per-user advisory lock: a blocking tombstone returns the sanitized account_deletion_pending conflict envelope with zero writes.';
