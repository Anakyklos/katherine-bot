-- 20240101000005_atomic_turn_commit.sql
-- Atomic turn commit RPC function (#271).
--
-- Implements the transactional commit of a complete turn unit:
-- 1. Acquire per-user lock (advisory or row-level)
-- 2. Check for existing request and handle idempotent replay
-- 3. Validate CAS (Compare-And-Swap) on profile revision
-- 4. Create profile if missing (race-safe upsert)
-- 5. Reject divergent payload for active requests; reclaim expired/expired-lease requests
-- 6. Insert user and assistant messages with stable IDs
-- 7. Update profile snapshots and increment revision exactly once
-- 8. Complete turn_requests with reproducible result
-- 9. Insert idempotent outbox events
-- 10. Commit all or rollback all
--
-- Design and rationale: docs/architecture/transactional-turn-schema.md
-- Issue: #271
-- Depends on: #270 (transactional schema foundation)
--
-- Security notes:
-- - Runs with SECURITY DEFINER as service_role
-- - EXECUTE revoked from PUBLIC/anon/authenticated
-- - All user_id values come from authenticated context
-- - No network, LLM, or embedding calls inside the transaction
-- - Payload validation leverages existing CHECK constraints
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
-- 1. RPC function: commit_turn
-- =================================================================
-- Main atomic commit function. Executes all steps in a single transaction.
--
-- Returns a JSON object with:
--   {
--     "user_id": text,
--     "request_id": uuid,
--     "committed_revision": bigint,
--     "user_message_chat_log_id": bigint,
--     "assistant_message_chat_log_id": bigint,
--     "user_message_id": uuid (the request_id, for symmetry),
--     "assistant_message_id": uuid (generated for assistant message),
--     "replay_payload": jsonb,
--     "outbox_events": jsonb (array of event records),
--     "created_at": timestamptz,
--     "completed_at": timestamptz
--   }
--
-- On conflict, returns:
--   {"error": {"code": "...", "message": "...", "expected_revision": ..., "actual_revision": ...}}
--
-- Error codes:
--   - revision_mismatch: profile revision doesn't match expected_revision
--   - request_payload_conflict: (user_id, request_id) already exists with different payload (for non-expired requests)
--   - profile_upsert_failed: could not create/lock profile
--   - message_insert_failed: could not insert messages
--   - turn_request_insert_failed: could not insert/update turn_request
--   - outbox_insert_failed: could not insert outbox events
--   - validation_failed: payload validation failed
--   - database_error: unexpected database error (sanitized)

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
    v_existing_user_message_chat_log_id bigint;
    v_existing_assistant_message_chat_log_id bigint;
    v_existing_replay_payload jsonb;
    v_existing_created_at timestamptz;
    v_existing_completed_at timestamptz;
    v_existing_lease_owner text;
    v_existing_lease_expires_at timestamptz;
    v_turn_request_id uuid;
    v_existing_turn_request_id uuid;
    
    -- Message IDs
    v_user_message_id bigint;
    v_assistant_message_id bigint;
    
    -- Outbox state
    v_outbox_results jsonb := '[]'::jsonb;
    
    -- Result building
    v_result jsonb;
    
    -- Error handling
    v_error_code text;
    v_error_message text;
    
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
    v_outbox_event_record jsonb;
    
    -- Snapshot validation forbidden keys
    v_snapshot_forbidden_keys text[] := ARRAY[
        'prompt', 'system_prompt', 'meta_cognition', 'internal_instructions',
        'message', 'user_message', 'assistant_message', 'content'
    ];
BEGIN
    -- =============================================================
    -- Step 0: Input validation (fail fast, before any writes)
    -- =============================================================
    
    -- Validate authenticated_user_id is not empty
    IF p_authenticated_user_id IS NULL OR p_authenticated_user_id = '' THEN
        RAISE EXCEPTION 'authenticated_user_id is required' USING ERRCODE = 'P0001';
    END IF;
    
    -- Validate request_id is not NULL
    IF p_request_id IS NULL THEN
        RAISE EXCEPTION 'request_id is required' USING ERRCODE = 'P0001';
    END IF;
    
    -- Validate expected_revision is not negative
    IF p_expected_revision < 0 THEN
        RAISE EXCEPTION 'expected_revision must be non-negative' USING ERRCODE = 'P0001';
    END IF;
    
    -- Validate messages are not NULL
    IF p_user_message IS NULL THEN
        RAISE EXCEPTION 'user_message is required' USING ERRCODE = 'P0001';
    END IF;
    IF p_assistant_message IS NULL THEN
        RAISE EXCEPTION 'assistant_message is required' USING ERRCODE = 'P0001';
    END IF;
    
    -- Validate payload_hash format
    IF p_payload_hash_sha256 IS NULL THEN
        RAISE EXCEPTION 'payload_hash_sha256 is required' USING ERRCODE = 'P0001';
    END IF;
    IF NOT (p_payload_hash_sha256 ~ '^[0-9a-f]{64}$') THEN
        RAISE EXCEPTION 'payload_hash_sha256 must be a 64-character hex string' USING ERRCODE = 'P0001';
    END IF;

    -- Validate replay_payload keys (must be allowed keys only, no forbidden keys)
    IF p_replay_payload IS NOT NULL AND jsonb_typeof(p_replay_payload) = 'object' THEN
        IF NOT public.jsonb_keys_subset_of(
            p_replay_payload,
            ARRAY['response', 'emotion_state', 'message_id', 'request_id', 'duration_ms']
        ) THEN
            RAISE EXCEPTION 'replay_payload has invalid keys' USING ERRCODE = 'P0001';
        END IF;
        IF public.jsonb_has_forbidden_key(p_replay_payload, v_snapshot_forbidden_keys) THEN
            RAISE EXCEPTION 'replay_payload contains forbidden keys' USING ERRCODE = 'P0001';
        END IF;
    END IF;

    -- Validate emotional_state snapshot (no forbidden keys)
    IF p_emotional_state IS NOT NULL AND jsonb_typeof(p_emotional_state) = 'object' THEN
        IF public.jsonb_has_forbidden_key(p_emotional_state, v_snapshot_forbidden_keys) THEN
            RAISE EXCEPTION 'emotional_state contains forbidden keys' USING ERRCODE = 'P0001';
        END IF;
    END IF;

    -- Validate relationship_state snapshot (no forbidden keys)
    IF p_relationship_state IS NOT NULL AND jsonb_typeof(p_relationship_state) = 'object' THEN
        IF public.jsonb_has_forbidden_key(p_relationship_state, v_snapshot_forbidden_keys) THEN
            RAISE EXCEPTION 'relationship_state contains forbidden keys' USING ERRCODE = 'P0001';
        END IF;
    END IF;

    -- =============================================================
    -- Step 1: Acquire per-user lock using advisory lock
    -- =============================================================
    PERFORM pg_advisory_xact_lock(hashtext(p_authenticated_user_id));

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
    -- This MUST run BEFORE CAS validation to allow exact replays to succeed
    -- =============================================================
    SELECT EXISTS (
        SELECT 1 FROM public.turn_requests
        WHERE user_id = p_authenticated_user_id AND request_id = p_request_id
    ) INTO v_request_exists;
    
    IF v_request_exists THEN
        -- Get the existing request to check payload hash, status, and all needed fields
        SELECT 
            id,
            payload_hash_sha256,
            status,
            committed_revision,
            user_message_chat_log_id,
            assistant_message_chat_log_id,
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
            v_existing_user_message_chat_log_id,
            v_existing_assistant_message_chat_log_id,
            v_existing_replay_payload,
            v_existing_created_at,
            v_existing_completed_at,
            v_existing_lease_owner,
            v_existing_lease_expires_at
        FROM public.turn_requests
        WHERE user_id = p_authenticated_user_id AND request_id = p_request_id;
        
        -- If the payload hash matches, this is an idempotent retry
        -- Return the EXACT stored result including committed_revision, outbox_events, etc.
        IF v_existing_payload_hash = p_payload_hash_sha256 THEN
            -- Reconstruct the outbox events for this request
            SELECT jsonb_agg(to_jsonb(outbox_events.*))
            INTO v_outbox_results
            FROM public.outbox_events
            WHERE turn_request_id = (
                SELECT id FROM public.turn_requests
                WHERE user_id = p_authenticated_user_id AND request_id = p_request_id
            );
            
            IF v_outbox_results IS NULL THEN
                v_outbox_results := '[]'::jsonb;
            END IF;
            
            -- Return the stored assistant_message_id from chat_logs for reproducibility
            v_result := jsonb_build_object(
                'user_id', p_authenticated_user_id,
                'request_id', p_request_id::text,
                'committed_revision', v_existing_committed_revision,
                'user_message_chat_log_id', v_existing_user_message_chat_log_id,
                'assistant_message_chat_log_id', v_existing_assistant_message_chat_log_id,
                'user_message_id', p_request_id::text,
                'assistant_message_id', (
                    SELECT id::text FROM public.chat_logs
                    WHERE user_id = p_authenticated_user_id 
                      AND id = v_existing_assistant_message_chat_log_id
                ),
                'replay_payload', COALESCE(v_existing_replay_payload, '{}'::jsonb),
                'outbox_events', v_outbox_results,
                'created_at', v_existing_created_at::text,
                'completed_at', COALESCE(v_existing_completed_at::text, v_existing_created_at::text)
            );
            RETURN v_result;
        ELSE
            -- Payload differs: conflict
            -- Check if the existing request can be reclaimed
            IF v_existing_status = 'expired' THEN
                -- For expired requests with different payload, we reclaim the existing row
                -- Use the existing turn_request_id for FK consistency
                v_turn_request_id := v_existing_turn_request_id;
                -- Continue to CAS validation and will UPDATE the existing row
            ELSIF v_existing_status = 'pending' AND v_existing_lease_expires_at IS NOT NULL AND v_existing_lease_expires_at <= v_now THEN
                -- Pending request with expired lease: treat as expired and reclaim
                v_turn_request_id := v_existing_turn_request_id;
                -- Continue to CAS validation and will UPDATE the existing row
            ELSE
                -- Active request with different payload - true conflict
                v_result := jsonb_build_object(
                    'error', jsonb_build_object(
                        'code', 'request_payload_conflict',
                        'message', 'Request ID already exists with different payload',
                        'expected_revision', p_expected_revision,
                        'request_id', p_request_id::text
                    )
                );
                RETURN v_result;
            END IF;
        END IF;
    END IF;

    -- =============================================================
    -- Step 4: Validate CAS: expected_revision must match current revision
    -- Only applied for new executions or expired request reclaim
    -- =============================================================
    IF v_current_revision <> p_expected_revision THEN
        v_result := jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'revision_mismatch',
                'message', 'Profile revision does not match expected value',
                'expected_revision', p_expected_revision,
                'actual_revision', v_current_revision
            )
        );
        RETURN v_result;
    END IF;

    -- =============================================================
    -- Step 5: Insert user message into chat_logs
    -- =============================================================
    INSERT INTO public.chat_logs (user_id, role, content, created_at)
    VALUES (p_authenticated_user_id, 'user', p_user_message, v_now)
    RETURNING id INTO v_user_message_id;

    -- =============================================================
    -- Step 6: Insert assistant message into chat_logs
    -- =============================================================
    INSERT INTO public.chat_logs (user_id, role, content, created_at)
    VALUES (p_authenticated_user_id, 'assistant', p_assistant_message, v_now)
    RETURNING id INTO v_assistant_message_id;

    -- =============================================================
    -- Step 7: Update or create profile with new snapshots and increment revision
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
    -- Step 8: Insert or Update turn_requests (capture DB id)
    -- For new requests: INSERT
    -- For expired request reclaim: UPDATE existing row with new payload
    -- =============================================================
    IF v_turn_request_id IS NOT NULL THEN
        -- Reclaiming an expired request: UPDATE the existing row
        UPDATE public.turn_requests
        SET 
            payload_hash_sha256 = p_payload_hash_sha256,
            status = 'completed',
            expected_revision = p_expected_revision,
            committed_revision = v_new_revision,
            user_message_chat_log_id = v_user_message_id,
            assistant_message_chat_log_id = v_assistant_message_id,
            replay_payload = COALESCE(p_replay_payload, '{}'::jsonb),
            updated_at = v_now,
            completed_at = v_now,
            error_code = NULL
        WHERE id = v_turn_request_id
            AND user_id = p_authenticated_user_id
            AND request_id = p_request_id;
    ELSE
        -- New request: INSERT
        INSERT INTO public.turn_requests (
            user_id, request_id, payload_hash_sha256, status,
            expected_revision, committed_revision,
            user_message_chat_log_id, assistant_message_chat_log_id,
            replay_payload, created_at, updated_at, completed_at
        ) VALUES (
            p_authenticated_user_id, p_request_id, p_payload_hash_sha256, 'completed',
            p_expected_revision, v_new_revision,
            v_user_message_id, v_assistant_message_id,
            COALESCE(p_replay_payload, '{}'::jsonb), v_now, v_now, v_now
        ) RETURNING id INTO v_turn_request_id;
    END IF;

    -- =============================================================
    -- Step 9: Insert outbox events (idempotent) referencing DB turn_request id
    -- =============================================================
    v_outbox_count := jsonb_array_length(p_outbox_events);
    
    FOR v_i IN 1..v_outbox_count LOOP
        v_event_obj := p_outbox_events->(v_i-1);
        v_event_type := v_event_obj->>'event_type';
        v_event_payload := v_event_obj->'payload';
        v_event_idempotency_key := v_event_obj->>'idempotency_key';
        
        -- Validate outbox event type
        IF v_event_type IS NULL OR NOT (v_event_type ~ '^[a-z0-9_]{1,64}$') THEN
            RAISE EXCEPTION 'Invalid outbox event_type' USING ERRCODE = 'P0001';
        END IF;
        
        -- Validate outbox payload (reuse existing validation function)
        IF v_event_payload IS NULL OR jsonb_typeof(v_event_payload) <> 'object' THEN
            RAISE EXCEPTION 'Outbox event payload must be a non-null JSON object' USING ERRCODE = 'P0001';
        END IF;
        
        IF NOT public.jsonb_keys_subset_of(
            v_event_payload,
            ARRAY['ref', 'request_id', 'turn_id', 'message_id', 'entity_id', 'kind', 'version']
        ) THEN
            RAISE EXCEPTION 'Outbox event payload has invalid keys' USING ERRCODE = 'P0001';
        END IF;
        
        IF public.jsonb_has_forbidden_key(
            v_event_payload,
            ARRAY['prompt', 'system_prompt', 'meta_cognition', 'internal_instructions',
                  'message', 'user_message', 'assistant_message', 'content']
        ) THEN
            RAISE EXCEPTION 'Outbox event payload contains forbidden keys' USING ERRCODE = 'P0001';
        END IF;
        
        IF NOT public.jsonb_outbox_payload_value_contract(v_event_payload) THEN
            RAISE EXCEPTION 'Outbox event payload violates value contract' USING ERRCODE = 'P0001';
        END IF;
        
        -- Validate idempotency key
        IF v_event_idempotency_key IS NULL OR NOT (v_event_idempotency_key ~ '^[A-Za-z0-9_.:-]{1,128}$') THEN
            RAISE EXCEPTION 'Invalid idempotency_key for outbox event' USING ERRCODE = 'P0001';
        END IF;
        
        -- Insert outbox event with reference to DB turn_request id (NOT p_request_id)
        -- This ensures the FK (user_id, turn_request_id) -> turn_requests(user_id, id) is valid
        INSERT INTO public.outbox_events (
            event_type, user_id, turn_request_id, payload, status,
            attempts, next_attempt_at, idempotency_key,
            contract_version, created_at, updated_at
        ) VALUES (
            v_event_type, p_authenticated_user_id, v_turn_request_id,
            v_event_payload, 'pending',
            0, v_now + INTERVAL '1 second', v_event_idempotency_key,
            1, v_now, v_now
        )
        RETURNING to_jsonb(outbox_events.*) INTO v_outbox_event_record;
        
        -- Accumulate outbox results
        v_outbox_results := v_outbox_results || jsonb_build_array(v_outbox_event_record);
    END LOOP;

    -- =============================================================
    -- Step 10: Build and return success result
    -- =============================================================
    v_result := jsonb_build_object(
        'user_id', p_authenticated_user_id,
        'request_id', p_request_id::text,
        'committed_revision', v_new_revision,
        'user_message_chat_log_id', v_user_message_id,
        'assistant_message_chat_log_id', v_assistant_message_id,
        'user_message_id', p_request_id::text,
        'assistant_message_id', v_assistant_message_id::text,
        'replay_payload', COALESCE(p_replay_payload, '{}'::jsonb),
        'outbox_events', v_outbox_results,
        'created_at', v_now::text,
        'completed_at', v_now::text
    );
    
    RETURN v_result;

EXCEPTION
    WHEN OTHERS THEN
        -- On any error, the transaction will roll back automatically
        -- Return a sanitized error WITHOUT leaking SQLERRM or SQLSTATE details
        v_result := jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'database_error',
                'message', 'internal database error'
            )
        );
        RETURN v_result;
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
) IS 'Atomic turn commit RPC (#271). Executes complete turn as a single transaction: profile CAS, message inserts, snapshot update, turn_request completion, outbox events. Fail-closed: any error rolls back everything. Replay-safe: idempotent retries return the exact stored result.';
