-- 20260808220000_privacy_data_operations.sql
-- Transactional, idempotent privacy primitives (#314).
--
-- Purely additive migration. Creates the durable privacy operations ledger
-- and four server-owned RPC primitives that run each privacy operation as a
-- single PostgreSQL transaction:
--
--   1. delete_history            atomic removal of turn history + derivatives
--   2. delete_memories           atomic removal of memories + candidates
--   3. reset_emotional_state     replace ONLY the emotional snapshot (v1)
--   4. reset_relationship_state  replace ONLY the relationship snapshot (v1)
--
-- Design and rationale: docs/architecture/user-data-lifecycle.md
-- Issue: #314 (chain #274, gate PROD-0 recovery #264)
-- Depends on: #271 (jsonb_snapshot_contract / commit_turn), #270, #265
--
-- Concurrency model (binding decision, mirrors commit_turn):
--   * every operation acquires the SAME per-user advisory transaction lock as
--     the turn commit: pg_advisory_xact_lock(hashtextextended(user_id, 0)).
--     Deletions/resets can therefore never interleave with a turn commit of
--     the same user, and distinct users are never globally serialized.
--   * the profile row is additionally locked FOR UPDATE to serialize writers.
--
-- Idempotency model (binding decision):
--   * every applied operation records a durable ledger row keyed by
--     (user_id, operation_id) storing the sanitized public result and the
--     SHA-256 fingerprint of the operation payload.
--   * replay of the same (user_id, operation_id) with the SAME operation and
--     payload returns the stored result WITHOUT re-running the mutation and
--     WITHOUT incrementing revision (no process-local cache: the ledger
--     survives restarts and is authoritative).
--   * the same operation_id with a DIFFERENT operation or payload fails with
--     a sanitized operation_conflict envelope.
--
-- Security posture (binding decisions):
--   * identity comes ONLY from p_authenticated_user_id (server-side boundary).
--     No user_id inside a payload/snapshot is ever trusted.
--   * the four RPCs are SECURITY DEFINER with fixed search_path = public and
--     EXECUTE granted to service_role only (revoked from PUBLIC, anon and
--     authenticated).
--   * the internal core/helpers have NO grants at all (owner postgres only).
--   * privacy_operations is RLS + FORCE RLS with no policies and NO table
--     grants (not even service_role): the RPCs are the only access path.
--   * results and errors are sanitized: only status/operation/operation_id/
--     user_id/revision and aggregate safe counts are returned. Message or
--     memory content, internal message/memory IDs, prompts, HMACs and raw SQL
--     are never returned nor logged.
--   * admission_reservations (anti-abuse ledger / quota) is deliberately NOT
--     touched by delete_history: quota cannot be bypassed by deleting history.

-- =================================================================
-- 0. PREFLIGHT: fail closed on missing dependencies and drift
-- =================================================================
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relname = 'privacy_operations'
    ) THEN
        RAISE EXCEPTION 'Cannot apply privacy operations: privacy_operations already exists (unexpected drift)'
            USING ERRCODE = '23514';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
          AND p.proname IN (
              'delete_history', 'delete_memories',
              'reset_emotional_state', 'reset_relationship_state',
              'privacy_apply_operation', 'privacy_operation_payload_sha256',
              'privacy_op_validation_error'
          )
    ) THEN
        RAISE EXCEPTION 'Cannot apply privacy operations: privacy functions already exist (unexpected drift)'
            USING ERRCODE = '23514';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_attribute a
        WHERE a.attrelid = 'public.profiles'::regclass
          AND a.attname = 'revision'
    ) THEN
        RAISE EXCEPTION 'Missing dependency: profiles.revision column (issue #270)'
            USING ERRCODE = '23514';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relname IN (
              'chat_logs', 'memories', 'archival_extractions',
              'turn_requests', 'outbox_events', 'admission_reservations'
          )
    ) THEN
        RAISE EXCEPTION 'Missing dependency: privacy target tables (issue #270/#271)'
            USING ERRCODE = '23514';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
          AND p.proname = 'jsonb_snapshot_contract'
    ) THEN
        RAISE EXCEPTION 'Missing dependency: public.jsonb_snapshot_contract (issue #271)'
            USING ERRCODE = '23514';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_extension e
        WHERE e.extname = 'pgcrypto'
    ) THEN
        RAISE EXCEPTION 'Missing dependency: pgcrypto extension (payload fingerprint)'
            USING ERRCODE = '23514';
    END IF;
END $$;

CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA extensions;

-- =================================================================
-- 1. Durable privacy operations ledger
-- =================================================================
-- One row per APPLIED operation. Keyed by (user_id, operation_id) so a user
-- can never observe or collide with another user's operation, and so a
-- server-provided operation_id is globally reusable across users without
-- cross-user interference. No FK to profiles: the ledger must durably record
-- outcomes even for users without a profile row yet (idempotency must
-- survive), and identity is server-provided at the same trust boundary as
-- the user_id text keys used everywhere else.
CREATE TABLE public.privacy_operations (
    user_id text NOT NULL,
    operation_id uuid NOT NULL,
    operation text NOT NULL,
    operation_payload_sha256 text NOT NULL,
    status text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT timezone('utc'::text, now()),
    result jsonb NOT NULL,

    CONSTRAINT privacy_operations_pkey
        PRIMARY KEY (user_id, operation_id),
    CONSTRAINT privacy_operations_user_id_check
        CHECK (
            char_length(user_id) BETWEEN 1 AND 128
            AND btrim(user_id) <> ''
        ),
    CONSTRAINT privacy_operations_operation_check
        CHECK (operation IN (
            'delete_history', 'delete_memories',
            'reset_emotional_state', 'reset_relationship_state'
        )),
    CONSTRAINT privacy_operations_payload_hash_check
        CHECK (operation_payload_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT privacy_operations_status_check
        CHECK (status = 'applied'),
    CONSTRAINT privacy_operations_result_check
        CHECK (jsonb_typeof(result) = 'object')
);

ALTER TABLE public.privacy_operations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.privacy_operations FORCE ROW LEVEL SECURITY;

-- Server-owned ledger: no role (not even service_role) gets table access.
-- The SECURITY DEFINER RPCs (owner postgres) are the only access path.
REVOKE ALL PRIVILEGES ON TABLE public.privacy_operations
    FROM PUBLIC, anon, authenticated, service_role;

-- =================================================================
-- 2. Payload fingerprint helper
-- =================================================================
-- Deterministic SHA-256 of the canonical jsonb serialization. jsonb::text is
-- canonical for a stored jsonb value, so identical incoming JSON always hashes
-- identically across processes and restarts. Used to bind an operation_id to
-- its exact operation payload: a divergent payload on replay is a conflict.
CREATE OR REPLACE FUNCTION public.privacy_operation_payload_sha256(
    p_payload jsonb
) RETURNS text
LANGUAGE sql
IMMUTABLE
SET search_path = extensions
AS $$
    SELECT pg_catalog.encode(extensions.digest(p_payload::text, 'sha256'), 'hex');
$$;

REVOKE ALL ON FUNCTION public.privacy_operation_payload_sha256(jsonb)
    FROM PUBLIC, anon, authenticated, service_role;

-- =================================================================
-- 3. Base validation helper (returns an error envelope or NULL)
-- =================================================================
CREATE OR REPLACE FUNCTION public.privacy_op_validation_error(
    p_user_id text,
    p_operation_id uuid,
    p_payload jsonb
) RETURNS jsonb
LANGUAGE plpgsql
IMMUTABLE
AS $$
BEGIN
    IF p_user_id IS NULL OR p_user_id = '' THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'validation_failed',
                'message', 'authenticated_user_id is required'
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
    IF p_payload IS NULL OR jsonb_typeof(p_payload) <> 'object' THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'validation_failed',
                'message', 'operation_payload must be a JSON object'
            )
        );
    END IF;
    RETURN NULL;
END;
$$;

REVOKE ALL ON FUNCTION public.privacy_op_validation_error(text, uuid, jsonb)
    FROM PUBLIC, anon, authenticated, service_role;

-- =================================================================
-- 4. Core: single transactional engine for all four operations
-- =================================================================
-- Executes every step of an operation in ONE transaction:
--   Step 0  fail-fast validation (stable, sanitized envelopes)
--   Step 1  per-user advisory lock (same boundary as commit_turn)
--   Step 2  durable idempotency check against the ledger
--   Step 3  profile row lock (serialize writers)
--   Step 4  apply the mutation (deletes or snapshot replacement)
--   Step 5  increment profiles.revision exactly once (invalidate prior turns)
--   Step 6  build the sanitized public result
--   Step 7  record the durable idempotency row
-- Any unexpected failure rolls back the whole transaction (WHEN OTHERS raises
-- a constant sanitized 'persistence error' P0001, never SQLERRM/SQLSTATE).
--
-- The resets reuse the EXISTING v1 snapshot contract (jsonb_snapshot_contract
-- from #271). No second emotional/relationship model is created in SQL: the
-- caller (server-side domain) produces EmotionalStateV1.neutral(...) /
-- RelationshipStateV1.neutral(...) snapshots and this function requires the
-- exact v1 allowlist/version already used by commit_turn.
CREATE OR REPLACE FUNCTION public.privacy_apply_operation(
    p_authenticated_user_id text,
    p_operation text,
    p_operation_id uuid,
    p_operation_payload jsonb
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_validation_error jsonb;
    v_payload_sha256 text;
    v_ledger public.privacy_operations%ROWTYPE;
    v_profile_exists boolean := FALSE;
    v_current_revision bigint := 0;
    v_new_revision bigint := 0;
    v_profiles_updated bigint := 0;
    v_count_chat_logs bigint := 0;
    v_count_turn_requests bigint := 0;
    v_count_outbox_events bigint := 0;
    v_count_archival bigint := 0;
    v_count_memories bigint := 0;
    v_result jsonb;
BEGIN
    -- =============================================================
    -- Step 0: input validation (fail fast, stable envelopes)
    -- =============================================================
    v_validation_error := public.privacy_op_validation_error(
        p_authenticated_user_id, p_operation_id, p_operation_payload
    );
    IF v_validation_error IS NOT NULL THEN
        RETURN v_validation_error;
    END IF;

    IF p_operation NOT IN (
        'delete_history', 'delete_memories',
        'reset_emotional_state', 'reset_relationship_state'
    ) THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'validation_failed',
                'message', 'operation is invalid'
            )
        );
    END IF;

    -- Resets require a VALID v1 snapshot produced by the domain. The
    -- snapshot is the operation payload and is bound to the operation_id by
    -- the fingerprint below.
    IF p_operation = 'reset_emotional_state'
       AND NOT public.jsonb_snapshot_contract(p_operation_payload, 'emotional') THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'validation_failed',
                'message', 'emotional_state snapshot violates the v1 contract'
            )
        );
    END IF;

    IF p_operation = 'reset_relationship_state'
       AND NOT public.jsonb_snapshot_contract(p_operation_payload, 'relationship') THEN
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'validation_failed',
                'message', 'relationship_state snapshot violates the v1 contract'
            )
        );
    END IF;

    v_payload_sha256 := public.privacy_operation_payload_sha256(p_operation_payload);

    -- =============================================================
    -- Step 1: per-user advisory transaction lock
    -- The EXACT same boundary used by commit_turn (#271): deletions/resets
    -- are serialized against turn commits of the same user, and different
    -- users never contend on a global lock.
    -- =============================================================
    PERFORM pg_advisory_xact_lock(hashtextextended(p_authenticated_user_id, 0));

    -- =============================================================
    -- Step 2: durable idempotency check
    -- A committed (user_id, operation_id) row with the same operation and
    -- payload fingerprint is an exact replay: return the stored result
    -- WITHOUT any mutation and WITHOUT any revision increment. Anything
    -- divergent is a sanitized conflict.
    -- =============================================================
    SELECT * INTO v_ledger
    FROM public.privacy_operations
    WHERE user_id = p_authenticated_user_id
      AND operation_id = p_operation_id;

    IF FOUND THEN
        IF v_ledger.operation = p_operation
           AND v_ledger.operation_payload_sha256 = v_payload_sha256 THEN
            RETURN v_ledger.result;
        END IF;
        RETURN jsonb_build_object(
            'error', jsonb_build_object(
                'code', 'operation_conflict',
                'message', 'operation_id already used with a different operation or payload'
            )
        );
    END IF;

    -- =============================================================
    -- Step 3: profile state (row lock serializes concurrent writers)
    -- A missing profile is legal: there is no data to mutate, the operation
    -- applies as a deterministic no-op and revision stays 0.
    -- =============================================================
    SELECT revision INTO v_current_revision
    FROM public.profiles
    WHERE user_id = p_authenticated_user_id
    FOR UPDATE;

    v_profile_exists := (v_current_revision IS NOT NULL);
    IF NOT v_profile_exists THEN
        v_current_revision := 0;
    END IF;

    -- =============================================================
    -- Step 4: apply the operation (all steps in this one transaction)
    -- delete_history removes chat_logs, turn_requests, their derived
    -- outbox_events and archival_extractions derived from the history.
    -- It deliberately NEVER touches memories, profiles/persona, snapshots
    -- or admission_reservations.
    -- delete_memories removes memories and archival/candidate material that
    -- may still represent ungoverned memory. It never touches chat or
    -- snapshots.
    -- Resets replace ONLY the target snapshot column with the validated v1
    -- snapshot produced by the domain.
    -- =============================================================
    IF p_operation = 'delete_history' THEN
        DELETE FROM public.outbox_events
        WHERE user_id = p_authenticated_user_id;
        GET DIAGNOSTICS v_count_outbox_events = ROW_COUNT;

        DELETE FROM public.turn_requests
        WHERE user_id = p_authenticated_user_id;
        GET DIAGNOSTICS v_count_turn_requests = ROW_COUNT;

        DELETE FROM public.archival_extractions
        WHERE user_id = p_authenticated_user_id;
        GET DIAGNOSTICS v_count_archival = ROW_COUNT;

        DELETE FROM public.chat_logs
        WHERE user_id = p_authenticated_user_id;
        GET DIAGNOSTICS v_count_chat_logs = ROW_COUNT;
    ELSIF p_operation = 'delete_memories' THEN
        DELETE FROM public.memories
        WHERE user_id = p_authenticated_user_id;
        GET DIAGNOSTICS v_count_memories = ROW_COUNT;

        DELETE FROM public.archival_extractions
        WHERE user_id = p_authenticated_user_id;
        GET DIAGNOSTICS v_count_archival = ROW_COUNT;
    ELSIF p_operation = 'reset_emotional_state' THEN
        UPDATE public.profiles
        SET emotional_state = p_operation_payload,
            updated_at = timezone('utc'::text, now())
        WHERE user_id = p_authenticated_user_id;
        GET DIAGNOSTICS v_profiles_updated = ROW_COUNT;
    ELSIF p_operation = 'reset_relationship_state' THEN
        UPDATE public.profiles
        SET relationship_state = p_operation_payload,
            updated_at = timezone('utc'::text, now())
        WHERE user_id = p_authenticated_user_id;
        GET DIAGNOSTICS v_profiles_updated = ROW_COUNT;
    END IF;

    -- =============================================================
    -- Step 5: revision invalidation (exactly once per applied operation)
    -- Any applied operation that has a profile invalidates prior turn
    -- computations exactly once. Replays never reach this step.
    -- =============================================================
    IF v_profile_exists THEN
        v_new_revision := v_current_revision + 1;
        UPDATE public.profiles
        SET revision = v_new_revision,
            updated_at = timezone('utc'::text, now())
        WHERE user_id = p_authenticated_user_id;
    END IF;

    -- =============================================================
    -- Step 6: build the sanitized public result
    -- Only status, operation, operation_id, user_id, revision and aggregate
    -- safe counts. Never message/memory content, internal IDs, prompts or
    -- HMACs.
    -- =============================================================
    v_result := jsonb_build_object(
        'status', 'applied',
        'operation', p_operation,
        'operation_id', p_operation_id::text,
        'user_id', p_authenticated_user_id,
        'revision', v_new_revision,
        'counts', jsonb_build_object(
            'chat_logs', v_count_chat_logs,
            'turn_requests', v_count_turn_requests,
            'outbox_events', v_count_outbox_events,
            'archival_extractions', v_count_archival,
            'memories', v_count_memories,
            'profiles', v_profiles_updated
        )
    );

    -- =============================================================
    -- Step 7: durable idempotency record (same transaction)
    -- =============================================================
    INSERT INTO public.privacy_operations (
        user_id, operation_id, operation, operation_payload_sha256,
        status, applied_at, result
    ) VALUES (
        p_authenticated_user_id, p_operation_id, p_operation,
        v_payload_sha256, 'applied', timezone('utc'::text, now()), v_result
    );

    RETURN v_result;
EXCEPTION
    WHEN OTHERS THEN
        -- Unexpected PostgreSQL failure. Never expose SQLERRM, SQLSTATE,
        -- constraint names or payload; propagate a constant sanitized message
        -- so the RPC fails (rollback is automatic) and the Python layer maps
        -- it to PersistenceError.
        RAISE EXCEPTION 'persistence error' USING ERRCODE = 'P0001';
END;
$$;

REVOKE ALL ON FUNCTION public.privacy_apply_operation(text, text, uuid, jsonb)
    FROM PUBLIC, anon, authenticated, service_role;

-- =================================================================
-- 5. Public RPC primitives (thin, server-owned wrappers)
-- =================================================================
-- Each wrapper fixes the operation name. The core performs validation
-- (including the v1 snapshot contract for resets), locking, idempotency,
-- mutation, revision bump and the durable ledger record.
CREATE OR REPLACE FUNCTION public.delete_history(
    p_authenticated_user_id text,
    p_operation_id uuid,
    p_operation_payload jsonb DEFAULT '{}'::jsonb
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    RETURN public.privacy_apply_operation(
        p_authenticated_user_id, 'delete_history', p_operation_id, p_operation_payload
    );
END;
$$;

CREATE OR REPLACE FUNCTION public.delete_memories(
    p_authenticated_user_id text,
    p_operation_id uuid,
    p_operation_payload jsonb DEFAULT '{}'::jsonb
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    RETURN public.privacy_apply_operation(
        p_authenticated_user_id, 'delete_memories', p_operation_id, p_operation_payload
    );
END;
$$;

CREATE OR REPLACE FUNCTION public.reset_emotional_state(
    p_authenticated_user_id text,
    p_operation_id uuid,
    p_operation_payload jsonb DEFAULT '{}'::jsonb
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    RETURN public.privacy_apply_operation(
        p_authenticated_user_id, 'reset_emotional_state', p_operation_id, p_operation_payload
    );
END;
$$;

CREATE OR REPLACE FUNCTION public.reset_relationship_state(
    p_authenticated_user_id text,
    p_operation_id uuid,
    p_operation_payload jsonb DEFAULT '{}'::jsonb
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    RETURN public.privacy_apply_operation(
        p_authenticated_user_id, 'reset_relationship_state', p_operation_id, p_operation_payload
    );
END;
$$;

-- =================================================================
-- 6. Grants: EXECUTE for service_role ONLY on the four public primitives
-- =================================================================
REVOKE ALL ON FUNCTION public.delete_history(text, uuid, jsonb)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.delete_history(text, uuid, jsonb) TO service_role;

REVOKE ALL ON FUNCTION public.delete_memories(text, uuid, jsonb)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.delete_memories(text, uuid, jsonb) TO service_role;

REVOKE ALL ON FUNCTION public.reset_emotional_state(text, uuid, jsonb)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.reset_emotional_state(text, uuid, jsonb) TO service_role;

REVOKE ALL ON FUNCTION public.reset_relationship_state(text, uuid, jsonb)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.reset_relationship_state(text, uuid, jsonb) TO service_role;

-- =================================================================
-- 7. Documentation comments
-- =================================================================
COMMENT ON FUNCTION public.delete_history(text, uuid, jsonb) IS
'Atomic, idempotent privacy primitive (#314). Removes turn history (chat_logs, turn_requests, derived outbox_events and archival_extractions) in one transaction. Preserves memories, persona/profile, snapshots and admission_reservations. Same per-user advisory lock as commit_turn. Replay-safe via the durable privacy_operations ledger; divergent payloads conflict sanitized. SECURITY DEFINER, service_role only.';

COMMENT ON FUNCTION public.delete_memories(text, uuid, jsonb) IS
'Atomic, idempotent privacy primitive (#314). Removes memories and archival/candidate material that may still represent ungoverned memory in one transaction. Preserves chat history and snapshots. Same per-user advisory lock as commit_turn. Replay-safe via the durable privacy_operations ledger; divergent payloads conflict sanitized. SECURITY DEFINER, service_role only.';

COMMENT ON FUNCTION public.reset_emotional_state(text, uuid, jsonb) IS
'Atomic, idempotent privacy primitive (#314). Replaces ONLY profiles.emotional_state with a validated v1 snapshot (produced by the domain, EmotionalStateV1.neutral) and increments profiles.revision exactly once. Preserves history, memories and the relationship snapshot. Same per-user advisory lock as commit_turn. Replay-safe via the durable privacy_operations ledger; divergent payloads conflict sanitized. SECURITY DEFINER, service_role only.';

COMMENT ON FUNCTION public.reset_relationship_state(text, uuid, jsonb) IS
'Atomic, idempotent privacy primitive (#314). Replaces ONLY profiles.relationship_state with a validated v1 snapshot (produced by the domain, RelationshipStateV1.neutral) and increments profiles.revision exactly once. Preserves history, memories and the emotional snapshot. Same per-user advisory lock as commit_turn. Replay-safe via the durable privacy_operations ledger; divergent payloads conflict sanitized. SECURITY DEFINER, service_role only.';
