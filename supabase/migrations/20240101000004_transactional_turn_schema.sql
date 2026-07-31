-- 20240101000004_transactional_turn_schema.sql
-- Transactional turn persistence foundation (#270).
--
-- Purely additive migration: introduces profiles.revision, the internal
-- turn_requests ledger and the durable outbox_events table. No active flow
-- of the ConversationEngine is wired to these objects yet.
--
-- Design and rationale: docs/architecture/transactional-turn-schema.md
--
-- Audit revisions (PR review #305):
--  * Composite FKs (user_id, message_id) / (user_id, turn_request_id) so a
--    request/event can never reference rows of another user.
--  * Recursive payload validation with an explicit allowlist, so prompts,
--    messages and internal fields are rejected even when nested.
--  * Exact, mutually exclusive outbox states (including next_attempt_at
--    being NULL while processing).
--  * Value contract for the outbox payload: reference/identifier fields are
--    scalar, sanitized and bounded; version is an integer in a defined
--    range. Arbitrary objects/arrays can never hide raw content under an
--    allowed key.
--  * Sanitized identifier bounds for lease_owner / idempotency_key.
--  * SECURITY DEFINER trigger function has EXECUTE revoked from
--    PUBLIC/anon/authenticated (fail-closed, granted to service_role only).

-- =================================================================
-- 0. PREFLIGHT: fail closed on unexpected schema drift
-- =================================================================
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_attribute a
        WHERE a.attrelid = 'public.profiles'::regclass
          AND a.attname = 'revision'
    ) THEN
        RAISE EXCEPTION 'Cannot apply transactional schema: profiles.revision already exists (unexpected drift)'
            USING ERRCODE = '23514';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relname IN ('turn_requests', 'outbox_events')
    ) THEN
        RAISE EXCEPTION 'Cannot apply transactional schema: internal tables already exist (unexpected drift)'
            USING ERRCODE = '23514';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
          AND p.proname IN (
              'jsonb_has_forbidden_key',
              'jsonb_keys_subset_of',
              'jsonb_outbox_payload_value_contract',
              'turn_requests_null_message_refs'
          )
    ) THEN
        RAISE EXCEPTION 'Cannot apply transactional schema: helper functions already exist (unexpected drift)'
            USING ERRCODE = '23514';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_trigger t
        JOIN pg_class c ON c.oid = t.tgrelid
        WHERE c.relname = 'chat_logs'
          AND t.tgname = 'turn_requests_message_refs_null_trigger'
    ) THEN
        RAISE EXCEPTION 'Cannot apply transactional schema: chat_logs trigger already exists (unexpected drift)'
            USING ERRCODE = '23514';
    END IF;
END $$;

-- =================================================================
-- 1. profiles.revision
-- =================================================================
-- Monotonic, per-user optimistic-concurrency counter. NOT NULL, default 0
-- backfills every existing profile deterministically with 0 and makes new
-- profiles (including future upserts that omit the column) start at 0.
--
-- updated_at policy: profiles.updated_at is maintained by the application
-- (server-side writes refresh it). No DB trigger is added here; the future
-- atomic commit flow must refresh updated_at whenever revision changes.
ALTER TABLE public.profiles
    ADD COLUMN revision bigint NOT NULL DEFAULT 0;

ALTER TABLE public.profiles
    ADD CONSTRAINT profiles_revision_non_negative_check
    CHECK (revision >= 0);

-- =================================================================
-- 2. Payload validation helpers (pure, immutable, no data access)
-- =================================================================
-- Used by the payload CHECK constraints below. Validation is fail-closed:
--  * jsonb_keys_subset_of()  — top-level keys must be within an explicit
--    allowlist (the minimal public contract for the stored document).
--  * jsonb_has_forbidden_key() — forbidden keys are rejected at ANY depth
--    (objects and arrays), so prompts / messages / internal fields can
--    never hide inside nested structures.
CREATE FUNCTION public.jsonb_has_forbidden_key(
    payload jsonb,
    forbidden text[]
) RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    v_key text;
    v_value jsonb;
    v_elem jsonb;
BEGIN
    IF payload IS NULL OR jsonb_typeof(payload) NOT IN ('object', 'array') THEN
        RETURN FALSE;
    END IF;

    IF jsonb_typeof(payload) = 'array' THEN
        FOR v_elem IN SELECT * FROM jsonb_array_elements(payload) LOOP
            IF public.jsonb_has_forbidden_key(v_elem, forbidden) THEN
                RETURN TRUE;
            END IF;
        END LOOP;
        RETURN FALSE;
    END IF;

    FOR v_key, v_value IN SELECT * FROM jsonb_each(payload) LOOP
        IF v_key = ANY(forbidden)
           OR public.jsonb_has_forbidden_key(v_value, forbidden) THEN
            RETURN TRUE;
        END IF;
    END LOOP;
    RETURN FALSE;
END;
$$;

CREATE FUNCTION public.jsonb_keys_subset_of(
    payload jsonb,
    allowed text[]
) RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    v_key text;
BEGIN
    IF payload IS NULL OR jsonb_typeof(payload) <> 'object' THEN
        RETURN FALSE;
    END IF;

    FOR v_key IN SELECT * FROM jsonb_object_keys(payload) LOOP
        IF NOT (v_key = ANY(allowed)) THEN
            RETURN FALSE;
        END IF;
    END LOOP;
    RETURN TRUE;
END;
$$;

-- Value contract for the outbox payload document. Key-name allowlists are
-- not enough: a value like {"ref": "<conteúdo bruto>"} or
-- {"ref": {"text": "..."}} would pass a pure key check. This helper
-- validates the TYPES and FORMATS of every allowed field:
--  * ref / request_id / turn_id / message_id / entity_id / kind: scalar
--    string, sanitized charset, bounded length (1..128). Arbitrary
--    objects/arrays can never be stored under these keys.
--  * version: JSON number that is an integer in [1, 1000].
CREATE FUNCTION public.jsonb_outbox_payload_value_contract(
    payload jsonb
) RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    v_key text;
    v_value jsonb;
    v_text text;
BEGIN
    IF payload IS NULL OR jsonb_typeof(payload) <> 'object' THEN
        RETURN FALSE;
    END IF;

    FOR v_key, v_value IN SELECT * FROM jsonb_each(payload) LOOP
        -- jsonb_each yields SQL NULL for a JSON null value; NULL guards are
        -- explicit because NULL comparisons never satisfy the checks below
        -- (fail-closed: null values are not valid contract values).
        IF v_value IS NULL THEN
            RETURN FALSE;
        END IF;
        IF v_key = 'version' THEN
            -- version must be an integer within the documented range.
            IF jsonb_typeof(v_value) <> 'number' THEN
                RETURN FALSE;
            END IF;
            v_text := v_value::text;
            IF NOT (v_text ~ '^[0-9]{1,4}$') THEN
                RETURN FALSE;
            END IF;
            IF v_text::integer < 1 OR v_text::integer > 1000 THEN
                RETURN FALSE;
            END IF;
        ELSE
            -- Reference/identifier fields are scalar, sanitized, bounded.
            IF jsonb_typeof(v_value) <> 'string' THEN
                RETURN FALSE;
            END IF;
            v_text := v_value #>> '{}';
            IF NOT (v_text ~ '^[A-Za-z0-9_.:-]{1,128}$') THEN
                RETURN FALSE;
            END IF;
        END IF;
    END LOOP;
    RETURN TRUE;
END;
$$;

-- Fail-closed grant posture for the helpers (they expose no data, but
-- clients must not be able to call server-owned internals).
REVOKE ALL ON FUNCTION public.jsonb_has_forbidden_key(jsonb, text[]) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.jsonb_keys_subset_of(jsonb, text[]) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.jsonb_outbox_payload_value_contract(jsonb) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.jsonb_has_forbidden_key(jsonb, text[]) TO service_role;
GRANT EXECUTE ON FUNCTION public.jsonb_keys_subset_of(jsonb, text[]) TO service_role;
GRANT EXECUTE ON FUNCTION public.jsonb_outbox_payload_value_contract(jsonb) TO service_role;

-- =================================================================
-- 3. turn_requests
-- =================================================================
-- Server-owned request ledger. One row per (user, request_id) claim that
-- the future atomic commit flow (issue #271) will use for idempotent,
-- revision-guarded execution and replay after connection loss.
CREATE TABLE public.turn_requests (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id text NOT NULL,
    request_id uuid NOT NULL,
    payload_hash_sha256 text NOT NULL,
    status text NOT NULL,
    lease_owner text,
    lease_expires_at timestamptz,
    expected_revision bigint NOT NULL DEFAULT 0,
    committed_revision bigint,
    user_message_chat_log_id bigint,
    assistant_message_chat_log_id bigint,
    replay_payload jsonb,
    error_code text,
    created_at timestamptz NOT NULL DEFAULT timezone('utc'::text, now()),
    updated_at timestamptz NOT NULL DEFAULT timezone('utc'::text, now()),
    completed_at timestamptz,

    CONSTRAINT turn_requests_user_id_request_id_key
        UNIQUE (user_id, request_id),
    -- Candidate key (user_id, id): enables the outbox composite FK so an
    -- event can only reference a request of the SAME user.
    CONSTRAINT turn_requests_user_id_id_key
        UNIQUE (user_id, id),
    CONSTRAINT turn_requests_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES public.profiles(user_id)
        ON DELETE CASCADE,
    -- Composite FKs enforce per-user isolation: a request can only point
    -- at messages of the same user. The baseline chat_logs already has
    -- UNIQUE (user_id, id) — see migration 20240101000000.
    -- ON DELETE SET NULL on a composite key would NULL user_id (NOT NULL),
    -- so a BEFORE DELETE trigger nulls ONLY the message references first;
    -- the SET NULL action then has nothing left to null.
    CONSTRAINT turn_requests_user_message_chat_log_id_fkey
        FOREIGN KEY (user_id, user_message_chat_log_id)
        REFERENCES public.chat_logs(user_id, id)
        ON DELETE SET NULL,
    CONSTRAINT turn_requests_assistant_message_chat_log_id_fkey
        FOREIGN KEY (user_id, assistant_message_chat_log_id)
        REFERENCES public.chat_logs(user_id, id)
        ON DELETE SET NULL,

    CONSTRAINT turn_requests_payload_hash_sha256_check
        CHECK (payload_hash_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT turn_requests_status_check
        CHECK (status IN ('pending', 'completed', 'expired')),
    CONSTRAINT turn_requests_expected_revision_check
        CHECK (expected_revision >= 0),
    CONSTRAINT turn_requests_committed_revision_check
        CHECK (committed_revision IS NULL OR committed_revision >= 0),
    CONSTRAINT turn_requests_error_code_check
        CHECK (error_code IS NULL OR error_code ~ '^[a-z0-9_]{1,64}$'),
    -- Sanitized worker identifier, max 64 chars (ADR section 3).
    CONSTRAINT turn_requests_lease_owner_check
        CHECK (lease_owner IS NULL OR lease_owner ~ '^[A-Za-z0-9_.:-]{1,64}$'),
    CONSTRAINT turn_requests_lease_pair_check
        CHECK (
            (lease_owner IS NULL AND lease_expires_at IS NULL)
            OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
        ),
    -- Status / lease / completion coherence. Fail-closed: a request can only
    -- exist in one fully determined shape.
    CONSTRAINT turn_requests_status_coherence_check
        CHECK (
            (status = 'pending' AND lease_owner IS NOT NULL
                AND lease_expires_at IS NOT NULL AND completed_at IS NULL
                AND committed_revision IS NULL AND replay_payload IS NULL
                AND error_code IS NULL)
            OR
            (status = 'completed' AND completed_at IS NOT NULL
                AND committed_revision IS NOT NULL AND replay_payload IS NOT NULL
                AND lease_owner IS NULL AND lease_expires_at IS NULL
                AND error_code IS NULL)
            OR
            (status = 'expired' AND completed_at IS NULL
                AND committed_revision IS NULL AND replay_payload IS NULL
                AND lease_owner IS NULL AND lease_expires_at IS NULL
                AND error_code IS NOT NULL)
        ),
    -- Replay payload: minimal PUBLIC result only. Explicit allowlist of
    -- top-level keys (public contract) plus a recursive forbidden-key check,
    -- so prompts / messages / internal instructions can never be stored,
    -- not even nested.
    CONSTRAINT turn_requests_replay_payload_check
        CHECK (
            replay_payload IS NULL
            OR (
                jsonb_typeof(replay_payload) = 'object'
                AND octet_length(replay_payload::text) <= 8192
                AND public.jsonb_keys_subset_of(
                    replay_payload,
                    ARRAY['response', 'emotion_state', 'message_id',
                          'request_id', 'duration_ms']
                )
                AND NOT public.jsonb_has_forbidden_key(
                    replay_payload,
                    ARRAY['prompt', 'system_prompt', 'meta_cognition',
                          'internal_instructions', 'message',
                          'user_message', 'assistant_message', 'content']
                )
            )
        )
);

-- BEFORE DELETE trigger on chat_logs: preserves the documented SET NULL
-- semantics for the composite message FKs. Runs as SECURITY DEFINER so it
-- can null references on the FORCE-RLS server-owned table even when the
-- deletion is performed by service_role.
CREATE FUNCTION public.turn_requests_null_message_refs() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    UPDATE public.turn_requests
       SET user_message_chat_log_id = NULL
     WHERE user_message_chat_log_id = OLD.id;
    UPDATE public.turn_requests
       SET assistant_message_chat_log_id = NULL
     WHERE assistant_message_chat_log_id = OLD.id;
    RETURN OLD;
END;
$$;

-- SECURITY DEFINER functions keep EXECUTE for PUBLIC by default. This
-- function runs exclusively through the trigger below (no runtime caller),
-- so EXECUTE is revoked from PUBLIC/anon/authenticated and granted only to
-- service_role for operational use — fail-closed privilege boundary.
REVOKE ALL ON FUNCTION public.turn_requests_null_message_refs() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.turn_requests_null_message_refs() TO service_role;

DROP TRIGGER IF EXISTS turn_requests_message_refs_null_trigger ON public.chat_logs;
CREATE TRIGGER turn_requests_message_refs_null_trigger
    BEFORE DELETE ON public.chat_logs
    FOR EACH ROW
    EXECUTE FUNCTION public.turn_requests_null_message_refs();

-- =================================================================
-- 4. outbox_events
-- =================================================================
-- Durable outbox for future atomic publication. Events are enqueued in the
-- same transaction as the turn commit; a future worker claims and delivers
-- them. No worker exists yet in this task.
CREATE TABLE public.outbox_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type text NOT NULL,
    contract_version integer NOT NULL DEFAULT 1,
    user_id text NOT NULL,
    turn_request_id uuid,
    payload jsonb NOT NULL,
    status text NOT NULL,
    attempts integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz,
    lease_owner text,
    lease_expires_at timestamptz,
    idempotency_key text NOT NULL,
    error_code text,
    created_at timestamptz NOT NULL DEFAULT timezone('utc'::text, now()),
    updated_at timestamptz NOT NULL DEFAULT timezone('utc'::text, now()),
    processed_at timestamptz,
    dead_lettered_at timestamptz,
    retention_until timestamptz,

    -- Idempotency key is unique per user: the same key for different users
    -- is allowed, and replaying a delivery within the same user is rejected.
    CONSTRAINT outbox_events_user_id_idempotency_key_key
        UNIQUE (user_id, idempotency_key),
    CONSTRAINT outbox_events_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES public.profiles(user_id)
        ON DELETE CASCADE,
    -- Composite FK: an event can only reference a request of the SAME user
    -- (candidate key turn_requests.user_id_id_key above).
    CONSTRAINT outbox_events_turn_request_id_fkey
        FOREIGN KEY (user_id, turn_request_id)
        REFERENCES public.turn_requests(user_id, id)
        ON DELETE CASCADE,

    CONSTRAINT outbox_events_event_type_check
        CHECK (event_type ~ '^[a-z0-9_]{1,64}$'),
    CONSTRAINT outbox_events_contract_version_check
        CHECK (contract_version > 0),
    CONSTRAINT outbox_events_status_check
        CHECK (status IN ('pending', 'processing', 'completed', 'failed', 'dead_letter')),
    CONSTRAINT outbox_events_attempts_check
        CHECK (attempts >= 0 AND attempts <= 10),
    CONSTRAINT outbox_events_error_code_check
        CHECK (error_code IS NULL OR error_code ~ '^[a-z0-9_]{1,64}$'),
    CONSTRAINT outbox_events_lease_owner_check
        CHECK (lease_owner IS NULL OR lease_owner ~ '^[A-Za-z0-9_.:-]{1,64}$'),
    -- Bounded idempotency key: prevents unbounded index/storage growth.
    CONSTRAINT outbox_events_idempotency_key_check
        CHECK (idempotency_key ~ '^[A-Za-z0-9_.:-]{1,128}$'),
    CONSTRAINT outbox_events_lease_pair_check
        CHECK (
            (lease_owner IS NULL AND lease_expires_at IS NULL)
            OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
        ),
    -- Status / lease / attempts / completion coherence. Fail-closed: each
    -- status has an exact, mutually exclusive shape — no field of another
    -- state can leak in (no next_attempt_at/dead_letter fields on
    -- completed, no missing error/retention on dead_letter, no failure
    -- fields on processing).
    CONSTRAINT outbox_events_status_coherence_check
        CHECK (
            (status = 'pending' AND processed_at IS NULL
                AND dead_lettered_at IS NULL AND retention_until IS NULL
                AND lease_owner IS NULL AND lease_expires_at IS NULL
                AND next_attempt_at IS NOT NULL AND attempts = 0
                AND error_code IS NULL)
            OR
            (status = 'processing' AND processed_at IS NULL
                AND dead_lettered_at IS NULL AND retention_until IS NULL
                AND next_attempt_at IS NULL
                AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL
                AND attempts BETWEEN 1 AND 10
                AND error_code IS NULL)
            OR
            (status = 'completed' AND processed_at IS NOT NULL
                AND dead_lettered_at IS NULL
                AND lease_owner IS NULL AND lease_expires_at IS NULL
                AND next_attempt_at IS NULL
                AND attempts BETWEEN 1 AND 10
                AND error_code IS NULL
                AND retention_until IS NOT NULL)
            OR
            (status = 'failed' AND processed_at IS NULL
                AND dead_lettered_at IS NULL AND retention_until IS NULL
                AND lease_owner IS NULL AND lease_expires_at IS NULL
                AND next_attempt_at IS NOT NULL
                AND attempts BETWEEN 1 AND 9
                AND error_code IS NOT NULL)
            OR
            (status = 'dead_letter' AND processed_at IS NULL
                AND lease_owner IS NULL AND lease_expires_at IS NULL
                AND next_attempt_at IS NULL
                AND attempts = 10 AND error_code IS NOT NULL
                AND dead_lettered_at IS NOT NULL
                AND retention_until IS NOT NULL)
        ),
    -- Outbox payload is a minimal, non-duplicating event document with an
    -- explicit allowlist and recursive forbidden-key validation. Messages,
    -- prompts, system instructions and metacognition can never be stored,
    -- not even nested.
    CONSTRAINT outbox_events_payload_check
        CHECK (
            jsonb_typeof(payload) = 'object'
            AND octet_length(payload::text) <= 8192
            AND public.jsonb_keys_subset_of(
                payload,
                ARRAY['ref', 'request_id', 'turn_id', 'message_id',
                      'entity_id', 'kind', 'version']
            )
            AND NOT public.jsonb_has_forbidden_key(
                payload,
                ARRAY['prompt', 'system_prompt', 'meta_cognition',
                      'internal_instructions', 'message',
                      'user_message', 'assistant_message', 'content']
            )
            AND public.jsonb_outbox_payload_value_contract(payload)
        )
);

-- =================================================================
-- 5. Indexes
-- =================================================================
-- turn_requests: uniqueness + replay + claim of expired leases + revision
CREATE INDEX turn_requests_user_id_created_at_idx
    ON public.turn_requests (user_id, created_at DESC);
CREATE INDEX turn_requests_status_lease_expiry_idx
    ON public.turn_requests (status, lease_expires_at);
CREATE INDEX turn_requests_user_committed_revision_idx
    ON public.turn_requests (user_id, committed_revision);

-- outbox_events: claim of available events + reclaim of expired leases
CREATE INDEX outbox_events_status_next_attempt_idx
    ON public.outbox_events (status, next_attempt_at);
CREATE INDEX outbox_events_status_lease_expiry_idx
    ON public.outbox_events (status, lease_expires_at);
CREATE INDEX outbox_events_turn_request_id_idx
    ON public.outbox_events (turn_request_id);

-- =================================================================
-- 6. RLS and grants (server-owned internal tables)
-- =================================================================
-- Both tables are internal, server-owned infrastructure. Authenticated
-- clients must never reach them. RLS + FORCE RLS with no policies, and no
-- grants to anon/authenticated/PUBLIC, mirror the guarantees delivered by
-- the #265 hardening. service_role keeps full CRUD (it bypasses RLS via
-- BYPASSRLS in Supabase), matching the pattern of the user-facing tables.
ALTER TABLE public.turn_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.turn_requests FORCE ROW LEVEL SECURITY;
ALTER TABLE public.outbox_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.outbox_events FORCE ROW LEVEL SECURITY;

REVOKE ALL PRIVILEGES ON TABLE public.turn_requests FROM PUBLIC;
REVOKE ALL PRIVILEGES ON TABLE public.turn_requests FROM anon, authenticated;
REVOKE ALL PRIVILEGES ON TABLE public.outbox_events FROM PUBLIC;
REVOKE ALL PRIVILEGES ON TABLE public.outbox_events FROM anon, authenticated;

REVOKE ALL PRIVILEGES ON TABLE public.turn_requests FROM service_role;
REVOKE ALL PRIVILEGES ON TABLE public.outbox_events FROM service_role;

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.turn_requests TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.outbox_events TO service_role;
