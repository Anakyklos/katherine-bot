-- 20240101000006_process_turn_replay.sql
-- Idempotent replay RPC for the ProcessTurn use case (#272).
--
-- Reads a previously committed turn from turn_requests and returns the SAME
-- canonical public contract produced by commit_turn (via the existing builder
-- public.commit_turn_build_result). Used by the active /chat path when the
-- admission ledger reports a repeated (user_id, request_id) so the persisted
-- result can be recovered WITHOUT loading context, running appraisal, calling
-- the provider, applying transitions or writing anything.
--
-- Result contract (structured, never raw):
--   * completed row -> canonical CommittedTurn envelope (same as commit_turn)
--   * pending row    -> {"status": "request_in_progress"}
--   * no row (only an admission reservation exists) -> {"status": "request_replay_unavailable"}
--   * expired row    -> {"status": "request_replay_unavailable"}
--   * invalid input or corrupt persisted contract -> sanitized error envelope
--     (validation_failed) or a raised constant sanitized error (P0001), never
--     SQLSTATE, constraint names, payload or raw error text.
--
-- Security posture (mirrors commit_turn):
--   * SECURITY DEFINER with fixed search_path = public, fully qualified objects
--   * EXECUTE revoked from PUBLIC/anon/authenticated; granted to service_role
--     only
--   * no new grants on turn_requests: runtime roles never get direct access
--   * identity comes exclusively from the authenticated user_id parameter;
--     results are filtered simultaneously by user_id AND request_id
--
-- Depends on: #271 (commit_turn / commit_turn_build_result, migration 05)
-- Issue: #272

-- =================================================================
-- 0. PREFLIGHT: verify dependencies
-- =================================================================
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND p.proname = 'commit_turn_build_result'
    ) THEN
        RAISE EXCEPTION 'Missing dependency: public.commit_turn_build_result (issue #271)'
            USING ERRCODE = '23514';
    END IF;
END $$;

-- =================================================================
-- 1. replay_committed_turn RPC
-- =================================================================
CREATE OR REPLACE FUNCTION public.replay_committed_turn(
    p_authenticated_user_id text,
    p_request_id uuid
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_row public.turn_requests%ROWTYPE;
BEGIN
    -- =============================================================
    -- Input validation (fail fast, stable envelopes)
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

    -- =============================================================
    -- Lookup filtered simultaneously by user AND request id
    -- =============================================================
    SELECT * INTO v_row
    FROM public.turn_requests
    WHERE user_id = p_authenticated_user_id
      AND request_id = p_request_id;

    IF NOT FOUND THEN
        -- The admission reservation exists, but no confirmed turn: replay
        -- is unavailable. Never returns SQLSTATE or raw error text.
        RETURN jsonb_build_object('status', 'request_replay_unavailable');
    END IF;

    IF v_row.status = 'completed' THEN
        -- The canonical builder guarantees the same public contract as a
        -- fresh commit (single authoritative replay format). Corrupt
        -- persisted contracts fail closed with a constant sanitized error.
        IF v_row.replay_payload IS NULL
           OR jsonb_typeof(v_row.replay_payload) <> 'object'
           OR jsonb_typeof(v_row.replay_payload->'response') <> 'string'
           OR (v_row.replay_payload->>'message_id') IS NULL
           OR NOT ((v_row.replay_payload->>'message_id')
                   ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
           OR v_row.committed_revision IS NULL
           OR v_row.completed_at IS NULL THEN
            RAISE EXCEPTION 'persistence error' USING ERRCODE = 'P0001';
        END IF;
        RETURN public.commit_turn_build_result(p_authenticated_user_id, p_request_id);
    END IF;

    -- pending: another worker is (or was) actively processing this request.
    IF v_row.status = 'pending' THEN
        RETURN jsonb_build_object('status', 'request_in_progress');
    END IF;

    -- expired: reservation exists but the turn was never confirmed.
    RETURN jsonb_build_object('status', 'request_replay_unavailable');
EXCEPTION
    WHEN OTHERS THEN
        -- Unexpected PostgreSQL failure or corrupt contract. Never expose
        -- SQLERRM, SQLSTATE, constraint names or payload; propagate a
        -- constant sanitized error so the RPC fails (rollback is automatic)
        -- and the Python layer maps it to PersistenceError.
        RAISE EXCEPTION 'persistence error' USING ERRCODE = 'P0001';
END;
$$;

-- =================================================================
-- 2. Grants for replay_committed_turn
-- =================================================================
REVOKE ALL ON FUNCTION public.replay_committed_turn(text, uuid)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.replay_committed_turn(text, uuid)
    TO service_role;

-- =================================================================
-- 3. Documentation comment
-- =================================================================
COMMENT ON FUNCTION public.replay_committed_turn(text, uuid) IS
'Idempotent replay RPC (#272). Returns the canonical public result of a previously committed turn without any writes, context loading, appraisal, provider call or transitions. Completed -> canonical CommittedTurn envelope; pending -> request_in_progress; missing/expired -> request_replay_unavailable. SECURITY DEFINER, service_role only, fixed search_path, sanitized failures.';
