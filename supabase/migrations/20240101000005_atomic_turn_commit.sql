-- 20240101000005_atomic_turn_commit.sql
-- Atomic turn commit RPC function (#271).
--
-- Implements the transactional commit of a complete turn unit:
-- 1. Input validation that returns stable, sanitized error envelopes
-- 2. Acquire per-user lock (64-bit advisory key, not 32-bit hashtext)
-- 3. Check for existing request and handle idempotent replay / lease reclaim
-- 4. Validate CAS (Compare-And-Swap) on profile revision
-- 5. Create profile if missing (race-safe upsert)
-- 6. Insert user and assistant messages
-- 7. Update profile snapshots and increment revision exactly once
-- 8. Complete turn_requests (new insert OR atomic conditional reclaim)
-- 9. Insert idempotent outbox events
-- 10. Build the public result from persisted rows (same path as replay)
-- 11. Commit all or roll back all
--
-- Design and rationale: docs/architecture/transactional-turn-schema.md
-- Issue: #271
-- Depends on: #270 (transactional schema foundation)
--
-- Public result contract (both fresh commit and replay):
--   {
--     "user_id": text,
--     "request_id": uuid (text),
--     "committed_revision": bigint,
--     "user_message_id": uuid (text, always equal to request_id),
--     "assistant_message_id": uuid (text, from replay_payload.message_id),
--     "replay_payload": jsonb (authoritative public result),
--     "outbox_events": [ {"id", "event_type", "idempotency_key",
--                         "turn_request_id", "contract_version"} ],
--     "created_at": timestamptz (text),
--     "completed_at": timestamptz (text)
--   }
-- Outbox references expose ONLY stable, immutable fields. Operational state
-- (status, attempts, next_attempt_at, leases, updated_at, processed_at,
-- error_code) is never returned. Internal chat_logs identifiers are not part
-- of the public contract: they can be nulled by pruning, so replay must never
-- depend on them.
--
-- Error envelopes (stable domain results):
--   {"error": {"code": "revision_mismatch", ...}}
--   {"error": {"code": "request_payload_conflict", ...}}
--   {"error": {"code": "request_in_progress", ...}}   (active lease, other worker)
--   {"error": {"code": "lease_conflict", ...}}        (claim/reclaim race)
--   {"error": {"code": "validation_failed", ...}}
-- Unexpected PostgreSQL failures are NOT returned as normal RPC success: the
-- handler raises a constant sanitized message so the failure propagates to the
-- client as a persistence error (rollback is automatic).
--
-- Security notes:
-- - Runs with SECURITY DEFINER as service_role
-- - EXECUTE revoked from PUBLIC/anon/authenticated
-- - All user_id values come from authenticated context; identity inside
--   snapshots is rejected (user_id / bond_label forbidden at any depth)
-- - No network, LLM, or embedding calls inside the transaction
-- - Fail-closed: any error rolls back the entire transaction

-- =================================================================
-- 0. PREFLIGHT: verify dependencies
-- =================================================================
DO $$
BEGIN
    -- Verify profiles.revision exists
    IF NOT EXISTS (
        SELECT 1
        FROM pg_attribute a
        WHERE a.attrelid = 'public.profiles'::regclass
          AND a.attname = 'revision'
    ) THEN
        RAISE EXCEPTION 'Missing dependency: profiles.revision column (issue #270)'
            USING ERRCODE = '23514';
    END IF;

    -- Verify turn_requests table exists
    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = 'turn_requests'
    ) THEN
        RAISE EXCEPTION 'Missing dependency: public.turn_requests table (issue #270)'
            USING ERRCODE = '23514';
    END IF;

    -- Verify outbox_events table exists
    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = 'outbox_events'
    ) THEN
        RAISE EXCEPTION 'Missing dependency: public.outbox_events table (issue #270)'
            USING ERRCODE = '23514';
    END IF;

    -- Verify helper functions exist
    IF NOT EXISTS (
        SELECT 1
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND p.proname = 'jsonb_has_forbidden_key'
    ) THEN
        RAISE EXCEPTION 'Missing dependency: public.jsonb_has_forbidden_key (issue #270)'
            USING ERRCODE = '23514';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND p.proname = 'jsonb_keys_subset_of'
    ) THEN
        RAISE EXCEPTION 'Missing dependency: public.jsonb_keys_subset_of (issue #270)'
            USING ERRCODE = '23514';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND p.proname = 'jsonb_outbox_payload_value_contract'
    ) THEN
        RAISE EXCEPTION 'Missing dependency: public.jsonb_outbox_payload_value_contract (issue #270)'
            USING ERRCODE = '23514';
    END IF;
END $$;

-- =================================================================
-- 0.1 Snapshot validation helper
-- =================================================================
-- Validates the fundamental structure of a persisted snapshot without
-- duplicating the whole emotional/relationship domain inside SQL:
--   * must be a JSON object (NULL passes; callers treat NULL as "no change")
--   * schema_version must be a JSON number that is an integer exactly 1
--     (rejects bool, string "1", floats and any other type)
--   * user_id / bond_label forbidden at ANY depth (identity is never stored)
--   * prompts / metacognition / messages / internal instructions forbidden
--   * size limited to 8192 bytes
--   * the minimal fundamental keys for each snapshot kind are required
CREATE OR REPLACE FUNCTION public.jsonb_snapshot_contract(
    payload jsonb,
    p_snapshot_kind text
) RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    v_schema_version jsonb;
BEGIN
    IF payload IS NULL THEN
        RETURN TRUE;
    END IF;

    IF jsonb_typeof(payload) <> 'object' THEN
        RETURN FALSE;
    END IF;

    -- schema_version must be a JSON number that is an integer.
    v_schema_version := payload->'schema_version';
    IF v_schema_version IS NULL OR jsonb_typeof(v_schema_version) <> 'number' THEN
        RETURN FALSE;
    END IF;
    -- jsonb has no separate integer type: reject floats / exponents / signs
    -- ("1.0", "1e0", "-1" must not pass as version 1).
    IF NOT ((payload->>'schema_version') ~ '^[0-9]+$') THEN
        RETURN FALSE;
    END IF;
    IF (payload->>'schema_version')::bigint <> 1 THEN
        RETURN FALSE;
    END IF;

    -- Identity and internal fields are forbidden at any depth.
    IF public.jsonb_has_forbidden_key(
        payload,
        ARRAY[
            'user_id', 'bond_label',
            'prompt', 'system_prompt', 'meta_cognition',
            'internal_instructions', 'message', 'user_message',
            'assistant_message', 'content'
        ]
    ) THEN
        RETURN FALSE;
    END IF;

    -- Size limit (mirrors the replay_payload / outbox payload bounds).
    IF octet_length(payload::text) > 8192 THEN
        RETURN FALSE;
    END IF;

    -- Minimal fundamental structure per snapshot kind.
    IF p_snapshot_kind = 'emotional' THEN
        IF NOT (payload ? 'pleasure')
           OR NOT (payload ? 'arousal')
           OR NOT (payload ? 'dominance') THEN
            RETURN FALSE;
        END IF;
    ELSIF p_snapshot_kind = 'relationship' THEN
        IF NOT (payload ? 'trust')
           OR NOT (payload ? 'affection')
           OR NOT (payload ? 'tension') THEN
            RETURN FALSE;
        END IF;
    END IF;

    RETURN TRUE;
END;
$$;

REVOKE ALL ON FUNCTION public.jsonb_snapshot_contract(jsonb, text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.jsonb_snapshot_contract(jsonb, text) TO service_role;

-- =================================================================
-- 0.2 Public result builder (single authoritative path)
-- =================================================================
-- Builds the canonical public result from PERSISTED rows. Used both by the
-- fresh commit path (after writing) and by the idempotent replay path, so the
-- initial response and every retry are assembled by exactly the same code.
-- Never reads chat_logs: message identifiers come from request_id and
-- replay_payload.message_id, both of which survive message pruning.
CREATE OR REPLACE FUNCTION public.commit_turn_build_result(
    p_user_id text,
    p_request_id uuid
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_row public.turn_requests%ROWTYPE;
    v_outbox jsonb;
BEGIN
    SELECT * INTO v_row
    FROM public.turn_requests
    WHERE user_id = p_user_id AND request_id = p_request_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'persistence error' USING ERRCODE = 'P0001';
    END IF;

    -- Stable, deterministic outbox references (ORDER BY id).
    SELECT COALESCE(
        jsonb_agg(
            jsonb_build_object(
                'id', e.id::text,
                'event_type', e.event_type,
                'idempotency_key', e.idempotency_key,
                'turn_request_id', e.turn_request_id::text,
                'contract_version', e.contract_version
            ) ORDER BY e.id
        ),
        '[]'::jsonb
    )
    INTO v_outbox
    FROM public.outbox_events e
    WHERE e.user_id = p_user_id AND e.turn_request_id = v_row.id;

    RETURN jsonb_build_object(
        'user_id', v_row.user_id,
        'request_id', v_row.request_id::text,
        'committed_revision', v_row.committed_revision,
        'user_message_id', v_row.request_id::text,
        'assistant_message_id', v_row.replay_payload->>'message_id',
        'replay_payload', COALESCE(v_row.replay_payload, '{}'::jsonb),
        'outbox_events', v_outbox,
        'created_at', v_row.created_at::text,
        'completed_at', COALESCE(v_row.completed_at::text, v_row.created_at::text)
    );
END;
$$;

REVOKE ALL ON FUNCTION public.commit_turn_build_result(text, uuid) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.commit_turn_build_result(text, uuid) TO service_role;

-- =================================================================
-- 1. RPC function: commit_turn
-- =================================================================
-- Main atomic commit function. Executes all steps in a single transaction.
--
-- Return contract documented in the header. Error codes:
--   - revision_mismatch: profile revision doesn't match expected_revision
--   - request_payload_conflict: (user_id, request_id) exists with a DIFFERENT
--     payload hash (any status)
--   - request_in_progress: request pending with an ACTIVE lease owned by
--     another worker (same payload hash)
--   - lease_conflict: claim/reclaim raced; the conditional UPDATE matched no
--     row, or a lease owner was required but not provided
--   - validation_failed: input violated the contract (sanitized message)
--   - persistence error (raised): unexpected PostgreSQL failure, sanitized

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
    p_lease_owner text DEFAULT NULL
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

    IF (p_replay_payload->>'response') IS NULL THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'validation_failed',
                'message', 'replay_payload must contain response'
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
        INSERT INTO public.profiles (
            user_id, persona_config, user_profile, relationship_state, emotional_state,
            revision, updated_at
        ) VALUES (
            p_authenticated_user_id, NULL, NULL,
            COALESCE(p_relationship_state, '{}'::jsonb),
            COALESCE(p_emotional_state, '{}'::jsonb),
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
            RETURN jsonb_build_object(
                'error', jsonb_build_object(
                    'code', 'lease_conflict',
                    'message', 'Request state changed; reclaim failed',
                    'expected_revision', p_expected_revision,
                    'request_id', p_request_id::text
                )
            );
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

    -- =============================================================
    -- Step 10: Build and return the public result from PERSISTED rows
    -- (identical to the replay path).
    -- =============================================================
    RETURN public.commit_turn_build_result(p_authenticated_user_id, p_request_id);

EXCEPTION
    WHEN OTHERS THEN
        -- Unexpected PostgreSQL failure. Never expose SQLERRM, SQLSTATE,
        -- constraint names or payload; propagate a constant sanitized message
        -- so the RPC fails (rollback is automatic) and the Python layer maps
        -- it to PersistenceError.
        RAISE EXCEPTION 'persistence error' USING ERRCODE = 'P0001';
END;
$$;


-- =================================================================
-- 2. Grants for commit_turn function
-- =================================================================
REVOKE ALL ON FUNCTION public.commit_turn(
    text, uuid, bigint, text, text, text, jsonb, jsonb, text, jsonb, jsonb, text
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.commit_turn(
    text, uuid, bigint, text, text, text, jsonb, jsonb, text, jsonb, jsonb, text
) TO service_role;

-- =================================================================
-- 3. Documentation comment
-- =================================================================
COMMENT ON FUNCTION public.commit_turn(
    text, uuid, bigint, text, text, text, jsonb, jsonb, text, jsonb, jsonb, text
) IS 'Atomic turn commit RPC (#271). Executes a complete turn as a single transaction: profile CAS, message inserts, snapshot update, turn_request completion, outbox events. Fail-closed: any error rolls back everything. Replay-safe: idempotent retries return the exact stored public result without writes. Lease/reclaim is protected by an atomic conditional UPDATE ... RETURNING.';
