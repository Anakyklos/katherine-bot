BEGIN;

CREATE EXTENSION IF NOT EXISTS pgtap;
SELECT plan(25);

-- =================================================================
-- 1. replay_committed_turn function shape and security posture
-- =================================================================

SELECT has_function(
    'public', 'replay_committed_turn', ARRAY['text', 'uuid'],
    'replay_committed_turn(text, uuid) exists'
);

SELECT is(
    (SELECT prosecdef
     FROM pg_proc
     WHERE proname = 'replay_committed_turn'
       AND pronamespace = 'public'::regnamespace
       AND pronargs = 2),
    true,
    'replay_committed_turn is SECURITY DEFINER'
);

SELECT is(
    (SELECT COALESCE(
        (SELECT setting
         FROM pg_proc p
         CROSS JOIN LATERAL unnest(proconfig) AS cfg(setting)
         WHERE p.proname = 'replay_committed_turn'
           AND p.pronamespace = 'public'::regnamespace
           AND p.pronargs = 2
           AND cfg.setting LIKE 'search_path=%'),
        '')
    ),
    'search_path=public',
    'replay_committed_turn has a fixed search_path = public'
);

-- =================================================================
-- 2. Execution privileges: only service_role
-- =================================================================

SELECT function_privs_are(
    'public', 'replay_committed_turn', ARRAY['text', 'uuid'],
    'anon', ARRAY[]::text[],
    'anon has no EXECUTE on replay_committed_turn'
);
SELECT function_privs_are(
    'public', 'replay_committed_turn', ARRAY['text', 'uuid'],
    'authenticated', ARRAY[]::text[],
    'authenticated has no EXECUTE on replay_committed_turn'
);
SELECT function_privs_are(
    'public', 'replay_committed_turn', ARRAY['text', 'uuid'],
    'service_role', ARRAY['EXECUTE'],
    'service_role has EXECUTE on replay_committed_turn'
);
SELECT ok(
    NOT has_function_privilege(
        'public', 'public.replay_committed_turn(text, uuid)', 'EXECUTE'
    ),
    'PUBLIC has no EXECUTE on replay_committed_turn'
);

-- =================================================================
-- 3. No runtime role gets direct access to turn_requests
-- =================================================================

SELECT table_privs_are(
    'public', 'turn_requests', 'anon', ARRAY[]::text[],
    'anon has no table privileges on turn_requests'
);
SELECT table_privs_are(
    'public', 'turn_requests', 'authenticated', ARRAY[]::text[],
    'authenticated has no table privileges on turn_requests'
);
SELECT ok(
    NOT has_table_privilege(
        'public', 'public.turn_requests',
        'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER'
    ),
    'PUBLIC has no privileges on turn_requests'
);

-- =================================================================
-- 4. Behavior against real rows (service_role)
-- =================================================================

-- Fixture user and request
INSERT INTO public.profiles (user_id, revision)
VALUES ('replay_tap_user', 0);

-- 4a. Completed turn returns the canonical contract
SELECT ok(
    (SELECT public.commit_turn(
        'replay_tap_user',
        'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'::uuid,
        0,
        'hello',
        'hi there',
        'a'::text || repeat('0', 63),
        jsonb_build_object(
            'schema_version', 1, 'pleasure', 0.1, 'arousal', 0.1,
            'dominance', 0.1, 'libido', 0.1, 'aggression', 0.1,
            'connection', 0.5, 'energy', 0.5, 'tension', 0.1,
            'coping_mode', 'HEALTHY', 'timestamp', 1700000000.0
        ),
        jsonb_build_object(
            'schema_version', 1, 'trust', 0.5, 'affection', 0.3,
            'tension', 0.1, 'triggers', '[]'::jsonb, 'timestamp', 1700000000.0
        ),
        'hi there',
        jsonb_build_object(
            'response', 'hi there',
            'message_id', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
        ),
        '[]'::jsonb
    ) IS NOT NULL),
    'commit_turn fixture succeeds'
);

SELECT ok(
    (SELECT (public.replay_committed_turn(
        'replay_tap_user', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'::uuid
    )->>'status') IS NULL),
    'completed replay returns the canonical envelope (no status marker)'
);

SELECT is(
    (SELECT public.replay_committed_turn(
        'replay_tap_user', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'::uuid
    )->>'user_id'),
    'replay_tap_user',
    'completed replay returns the canonical user_id'
);

SELECT is(
    (SELECT public.replay_committed_turn(
        'replay_tap_user', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'::uuid
    )->>'request_id'),
    'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
    'completed replay returns the canonical request_id'
);

SELECT is(
    (SELECT public.replay_committed_turn(
        'replay_tap_user', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'::uuid
    )->'replay_payload'->>'message_id'),
    'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
    'completed replay returns replay_payload.message_id'
);

-- Repeated replays are byte-equivalent
SELECT is(
    (SELECT public.replay_committed_turn(
        'replay_tap_user', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'::uuid
    )),
    (SELECT public.replay_committed_turn(
        'replay_tap_user', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'::uuid
    )),
    'repeated replays return equivalent bytes'
);

-- 4b. Replay does not alter any table
SELECT is(
    (SELECT count(*)::integer FROM public.turn_requests WHERE user_id = 'replay_tap_user'),
    1,
    'turn_requests unchanged after replay'
);
SELECT is(
    (SELECT count(*)::integer FROM public.chat_logs WHERE user_id = 'replay_tap_user'),
    2,
    'chat_logs unchanged after replay'
);
SELECT is(
    (SELECT revision FROM public.profiles WHERE user_id = 'replay_tap_user')::bigint,
    1::bigint,
    'profile revision unchanged after replay'
);

-- 4c. User A cannot retrieve user B turn
SELECT is(
    (SELECT public.replay_committed_turn(
        'replay_other_user', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'::uuid
    )->>'status'),
    'request_replay_unavailable',
    'another user cannot replay this turn (structured result)'
);

-- 4d. Missing request returns structured result
SELECT is(
    (SELECT public.replay_committed_turn(
        'replay_tap_user', 'cccccccc-cccc-cccc-cccc-cccccccccccc'::uuid
    )->>'status'),
    'request_replay_unavailable',
    'missing request returns request_replay_unavailable'
);

-- 4e. Pending request is not treated as completed
INSERT INTO public.turn_requests (
    user_id, request_id, payload_hash_sha256, status,
    lease_owner, lease_expires_at
) VALUES (
    'replay_tap_user', 'dddddddd-dddd-dddd-dddd-dddddddddddd'::uuid,
    'b'::text || repeat('0', 63), 'pending',
    'worker-x', timezone('utc'::text, now()) + INTERVAL '1 hour'
);

SELECT is(
    (SELECT public.replay_committed_turn(
        'replay_tap_user', 'dddddddd-dddd-dddd-dddd-dddddddddddd'::uuid
    )->>'status'),
    'request_in_progress',
    'pending request returns request_in_progress'
);

-- 4f. Expired request is not treated as completed
INSERT INTO public.turn_requests (
    user_id, request_id, payload_hash_sha256, status, error_code
) VALUES (
    'replay_tap_user', 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee'::uuid,
    'c'::text || repeat('0', 63), 'expired', 'timeout'
);

SELECT is(
    (SELECT public.replay_committed_turn(
        'replay_tap_user', 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee'::uuid
    )->>'status'),
    'request_replay_unavailable',
    'expired request returns request_replay_unavailable'
);

-- 4g. Invalid identity fails sanitized (never leaks raw input)
SELECT is(
    (SELECT public.replay_committed_turn('', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'::uuid)
        ->'error'->>'code'),
    'validation_failed',
    'empty authenticated_user_id fails with validation_failed'
);

SELECT ok(
    (SELECT public.replay_committed_turn('', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'::uuid)
        #>> '{error,message}') LIKE '%required%',
    'validation failure message is sanitized (no raw input echoed)'
);

-- Cleanup (chat_logs first: profiles is FK-referenced by messages)
DELETE FROM public.chat_logs WHERE user_id = 'replay_tap_user';
DELETE FROM public.profiles WHERE user_id = 'replay_tap_user';

SELECT * FROM finish();
ROLLBACK;
