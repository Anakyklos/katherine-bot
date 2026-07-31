-- 20240101000004_transactional_turn_schema.sql
-- Transactional turn persistence foundation (#270).
--
-- Purely additive migration: introduces profiles.revision, the internal
-- turn_requests ledger and the durable outbox_events table. No active flow
-- of the ConversationEngine is wired to these objects yet.
--
-- Design and rationale: docs/architecture/transactional-turn-schema.md

-- =================================================================
-- 0. PREFLIGHT: fail closed on unexpected schema drift
-- =================================================================
-- If any of the objects already exist, the database has drifted from the
-- expected migration sequence. Fail loudly instead of silently re-creating
-- or altering objects that a later migration owns.
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
-- 2. turn_requests
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
    CONSTRAINT turn_requests_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES public.profiles(user_id)
        ON DELETE CASCADE,
    CONSTRAINT turn_requests_user_message_chat_log_id_fkey
        FOREIGN KEY (user_message_chat_log_id) REFERENCES public.chat_logs(id)
        ON DELETE SET NULL,
    CONSTRAINT turn_requests_assistant_message_chat_log_id_fkey
        FOREIGN KEY (assistant_message_chat_log_id) REFERENCES public.chat_logs(id)
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
    -- Replay payload stores ONLY the minimal public result needed to replay
    -- a response after connection loss. Prompts, system instructions and
    -- metacognition are forbidden inside the stored document.
    CONSTRAINT turn_requests_replay_payload_check
        CHECK (
            replay_payload IS NULL
            OR (
                jsonb_typeof(replay_payload) = 'object'
                AND octet_length(replay_payload::text) <= 8192
                AND NOT (replay_payload ? 'prompt')
                AND NOT (replay_payload ? 'system_prompt')
                AND NOT (replay_payload ? 'meta_cognition')
                AND NOT (replay_payload ? 'internal_instructions')
            )
        )
);

-- =================================================================
-- 3. outbox_events
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
    CONSTRAINT outbox_events_turn_request_id_fkey
        FOREIGN KEY (turn_request_id) REFERENCES public.turn_requests(id)
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
    CONSTRAINT outbox_events_lease_pair_check
        CHECK (
            (lease_owner IS NULL AND lease_expires_at IS NULL)
            OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
        ),
    -- Status / lease / attempts / completion coherence. Fail-closed: each
    -- status has an exact, fully determined shape.
    CONSTRAINT outbox_events_status_coherence_check
        CHECK (
            (status = 'pending' AND processed_at IS NULL
                AND lease_owner IS NULL AND lease_expires_at IS NULL
                AND next_attempt_at IS NOT NULL AND attempts = 0
                AND error_code IS NULL)
            OR
            (status = 'processing' AND processed_at IS NULL
                AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL
                AND attempts >= 1)
            OR
            (status = 'completed' AND processed_at IS NOT NULL
                AND lease_owner IS NULL AND lease_expires_at IS NULL
                AND attempts >= 1 AND error_code IS NULL)
            OR
            (status = 'failed' AND processed_at IS NULL
                AND lease_owner IS NULL AND lease_expires_at IS NULL
                AND next_attempt_at IS NOT NULL
                AND attempts BETWEEN 1 AND 9 AND error_code IS NOT NULL)
            OR
            (status = 'dead_letter' AND processed_at IS NULL
                AND lease_owner IS NULL AND lease_expires_at IS NULL
                AND dead_lettered_at IS NOT NULL AND attempts = 10)
        ),
    -- Outbox payload is a minimal, non-duplicating event document. Messages,
    -- prompts, system instructions and metacognition must never be persisted.
    CONSTRAINT outbox_events_payload_check
        CHECK (
            jsonb_typeof(payload) = 'object'
            AND octet_length(payload::text) <= 8192
            AND NOT (payload ? 'prompt')
            AND NOT (payload ? 'system_prompt')
            AND NOT (payload ? 'meta_cognition')
            AND NOT (payload ? 'internal_instructions')
        )
);

-- =================================================================
-- 4. Indexes
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
-- 5. RLS and grants (server-owned internal tables)
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
