BEGIN;

CREATE EXTENSION IF NOT EXISTS pgtap;
SELECT no_plan();

-- -----------------------------------------------------------------
-- Structure, constraints, indexes, RLS and privileges
-- -----------------------------------------------------------------
SELECT has_table('public', 'admission_reservations', 'admission ledger table exists');

SELECT has_column('public', 'admission_reservations', 'user_id', 'has user_id');
SELECT col_not_null('public', 'admission_reservations', 'user_id', 'user_id not null');
SELECT col_type_is('public', 'admission_reservations', 'user_id', 'text', 'user_id text');
SELECT has_column('public', 'admission_reservations', 'request_id', 'has request_id');
SELECT col_not_null('public', 'admission_reservations', 'request_id', 'request_id not null');
SELECT col_type_is('public', 'admission_reservations', 'request_id', 'uuid', 'request_id uuid');
SELECT has_column('public', 'admission_reservations', 'message_hmac_sha256', 'has message hmac');
SELECT col_not_null('public', 'admission_reservations', 'message_hmac_sha256', 'message hmac not null');
SELECT col_type_is('public', 'admission_reservations', 'message_hmac_sha256', 'text', 'message hmac text');
SELECT has_column('public', 'admission_reservations', 'network_hmac_sha256', 'has network hmac');
SELECT col_not_null('public', 'admission_reservations', 'network_hmac_sha256', 'network hmac not null');
SELECT col_type_is('public', 'admission_reservations', 'network_hmac_sha256', 'text', 'network hmac text');
SELECT has_column('public', 'admission_reservations', 'estimated_units', 'has estimated units');
SELECT col_not_null('public', 'admission_reservations', 'estimated_units', 'estimated units not null');
SELECT col_type_is('public', 'admission_reservations', 'estimated_units', 'integer', 'estimated units integer');
SELECT has_column('public', 'admission_reservations', 'reserved_at', 'has reserved_at');
SELECT col_not_null('public', 'admission_reservations', 'reserved_at', 'reserved_at not null');
SELECT col_type_is('public', 'admission_reservations', 'reserved_at', 'timestamp with time zone', 'reserved_at timestamptz');

SELECT has_pk('public', 'admission_reservations', 'ledger has primary key');
SELECT is(
  (SELECT pg_get_constraintdef(oid) FROM pg_constraint
   WHERE conname = 'admission_reservations_pkey'),
  'PRIMARY KEY (user_id, request_id)',
  'primary key is user_id plus request_id'
);
SELECT ok(EXISTS(SELECT 1 FROM pg_constraint WHERE conname = 'admission_reservations_user_id_check'), 'has user check');
SELECT ok(EXISTS(SELECT 1 FROM pg_constraint WHERE conname = 'admission_reservations_message_hmac_check'), 'has message hmac check');
SELECT ok(EXISTS(SELECT 1 FROM pg_constraint WHERE conname = 'admission_reservations_network_hmac_check'), 'has network hmac check');
SELECT ok(EXISTS(SELECT 1 FROM pg_constraint WHERE conname = 'admission_reservations_estimated_units_check'), 'has units check');
SELECT ok(
  (SELECT pg_get_expr(conbin, conrelid) LIKE '%btrim(user_id)%'
   FROM pg_constraint WHERE conname = 'admission_reservations_user_id_check'),
  'user check rejects whitespace-only ids'
);
SELECT ok(
  (SELECT pg_get_expr(conbin, conrelid) LIKE '%^[0-9a-f]{64}$%'
   FROM pg_constraint WHERE conname = 'admission_reservations_message_hmac_check'),
  'message hmac is lowercase hex64'
);
SELECT ok(
  (SELECT pg_get_expr(conbin, conrelid) LIKE '%^[0-9a-f]{64}$%'
   FROM pg_constraint WHERE conname = 'admission_reservations_network_hmac_check'),
  'network hmac is lowercase hex64'
);
SELECT ok(
  (SELECT pg_get_expr(conbin, conrelid) LIKE '%estimated_units%1%6000%'
   FROM pg_constraint WHERE conname = 'admission_reservations_estimated_units_check'),
  'units range is 1 through 6000'
);

SELECT has_index('public', 'admission_reservations', 'admission_reservations_user_time_idx', 'user-time index exists');
SELECT has_index('public', 'admission_reservations', 'admission_reservations_network_time_idx', 'network-time index exists');
SELECT has_index('public', 'admission_reservations', 'admission_reservations_time_idx', 'global-time index exists');
SELECT is(
  pg_get_indexdef('public.admission_reservations_user_time_idx'::regclass),
  'CREATE INDEX admission_reservations_user_time_idx ON public.admission_reservations USING btree (user_id, reserved_at DESC)',
  'user-time index has exact descending definition'
);
SELECT is(
  pg_get_indexdef('public.admission_reservations_network_time_idx'::regclass),
  'CREATE INDEX admission_reservations_network_time_idx ON public.admission_reservations USING btree (network_hmac_sha256, reserved_at DESC)',
  'network-time index has exact descending definition'
);
SELECT is(
  pg_get_indexdef('public.admission_reservations_time_idx'::regclass),
  'CREATE INDEX admission_reservations_time_idx ON public.admission_reservations USING btree (reserved_at DESC)',
  'global-time index has exact descending definition'
);

SELECT ok((SELECT relrowsecurity FROM pg_class WHERE oid = 'public.admission_reservations'::regclass), 'RLS enabled');
SELECT ok((SELECT relforcerowsecurity FROM pg_class WHERE oid = 'public.admission_reservations'::regclass), 'FORCE RLS enabled');
SELECT policies_are('public', 'admission_reservations', ARRAY[]::text[], 'ledger has no policies');
SELECT table_privs_are('public', 'admission_reservations', 'anon', ARRAY[]::text[]);
SELECT table_privs_are('public', 'admission_reservations', 'authenticated', ARRAY[]::text[]);
SELECT table_privs_are('public', 'admission_reservations', 'service_role', ARRAY[]::text[]);
SELECT ok(
  NOT has_table_privilege('public', 'public.admission_reservations', 'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER'),
  'PUBLIC has no direct table privileges'
);

-- -----------------------------------------------------------------
-- RPC metadata and hardening
-- -----------------------------------------------------------------
SELECT has_function(
  'public', 'reserve_admission',
  ARRAY['text', 'uuid', 'text', 'text', 'integer'],
  'reserve_admission exists with exact argument types'
);
SELECT ok(
  (SELECT p.prosecdef FROM pg_proc p
   WHERE p.oid = 'public.reserve_admission(text,uuid,text,text,integer)'::regprocedure),
  'reserve_admission is SECURITY DEFINER'
);
SELECT ok(
  (SELECT array_to_string(p.proconfig, ',') IN ('search_path=', 'search_path=""')
   FROM pg_proc p
   WHERE p.oid = 'public.reserve_admission(text,uuid,text,text,integer)'::regprocedure),
  'reserve_admission has an empty fixed search_path'
);
SELECT function_privs_are('public', 'reserve_admission', ARRAY['text','uuid','text','text','integer'], 'anon', ARRAY[]::text[]);
SELECT function_privs_are('public', 'reserve_admission', ARRAY['text','uuid','text','text','integer'], 'authenticated', ARRAY[]::text[]);
SELECT function_privs_are('public', 'reserve_admission', ARRAY['text','uuid','text','text','integer'], 'service_role', ARRAY['EXECUTE']);
SELECT ok(
  NOT has_function_privilege('public', 'public.reserve_admission(text,uuid,text,text,integer)', 'EXECUTE'),
  'PUBLIC has no EXECUTE on reserve_admission'
);
SELECT ok(
  (SELECT pg_get_functiondef(p.oid) LIKE '%pg_catalog.pg_advisory_xact_lock(1262572616, 1094995249)%'
   FROM pg_proc p WHERE p.oid = 'public.reserve_admission(text,uuid,text,text,integer)'::regprocedure),
  'RPC uses the documented global transaction advisory lock'
);
SELECT ok(
  (SELECT pg_get_functiondef(p.oid) NOT LIKE '%EXECUTE %'
          AND pg_get_functiondef(p.oid) NOT LIKE '%format(%'
          AND pg_get_functiondef(p.oid) NOT LIKE '%quote_ident%'
          AND pg_get_functiondef(p.oid) NOT LIKE '%quote_literal%'
   FROM pg_proc p WHERE p.oid = 'public.reserve_admission(text,uuid,text,text,integer)'::regprocedure),
  'RPC contains no dynamic SQL'
);
SELECT is(
  (SELECT pg_get_function_result('public.reserve_admission(text,uuid,text,text,integer)'::regprocedure)),
  'TABLE(decision text, retry_after_seconds integer)',
  'RPC result contract is exact'
);

-- -----------------------------------------------------------------
-- Behavior and deterministic precedence
-- -----------------------------------------------------------------
TRUNCATE public.admission_reservations;
SELECT is(
  (SELECT decision FROM public.reserve_admission('u-basic','00000000-0000-4000-8000-000000000001',repeat('a',64),repeat('b',64),100)),
  'admitted', 'valid request is admitted'
);
SELECT is(
  (SELECT retry_after_seconds FROM public.reserve_admission('u-basic','00000000-0000-4000-8000-000000000001',repeat('a',64),repeat('b',64),100)),
  0, 'replay retry is zero'
);
SELECT is((SELECT count(*)::integer FROM public.admission_reservations WHERE user_id='u-basic'), 1, 'admitted request creates one row');
SELECT is(
  (SELECT decision FROM public.reserve_admission('u-basic','00000000-0000-4000-8000-000000000001',repeat('a',64),repeat('c',64),500)),
  'request_replay_unavailable', 'same id and message is replay unavailable regardless of network or units'
);
SELECT is(
  (SELECT decision FROM public.reserve_admission('u-basic','00000000-0000-4000-8000-000000000001',repeat('c',64),repeat('b',64),100)),
  'request_id_conflict', 'same id with different message is conflict'
);
SELECT is((SELECT count(*)::integer FROM public.admission_reservations WHERE user_id='u-basic'), 1, 'replay and conflict consume no quota');

SELECT is(
  (SELECT decision FROM public.reserve_admission('u-basic','00000000-0000-4000-8000-000000000002',repeat('a',64),repeat('b',64),100)),
  'admitted', 'same message with a new request id is a new reservation'
);
SELECT is((SELECT count(*)::integer FROM public.admission_reservations WHERE user_id='u-basic'), 2, 'same message with new id consumes quota');

SELECT is(
  (SELECT decision FROM public.reserve_admission('u-other','00000000-0000-4000-8000-000000000001',repeat('d',64),repeat('e',64),100)),
  'admitted', 'same UUID is isolated between users'
);
SELECT is((SELECT count(*)::integer FROM public.admission_reservations WHERE request_id='00000000-0000-4000-8000-000000000001'), 2, 'same UUID stores one row per user');

TRUNCATE public.admission_reservations;
SELECT is((SELECT decision FROM public.reserve_admission('',gen_random_uuid(),repeat('a',64),repeat('b',64),1)), 'invalid_admission_input', 'empty user rejected');
SELECT is((SELECT decision FROM public.reserve_admission('   ',gen_random_uuid(),repeat('a',64),repeat('b',64),1)), 'invalid_admission_input', 'whitespace user rejected');
SELECT is((SELECT decision FROM public.reserve_admission('u',gen_random_uuid(),repeat('A',64),repeat('b',64),1)), 'invalid_admission_input', 'uppercase hmac rejected');
SELECT is((SELECT decision FROM public.reserve_admission('u',gen_random_uuid(),repeat('a',64),repeat('b',64),6001)), 'invalid_admission_input', 'oversized units rejected');
SELECT is((SELECT count(*)::integer FROM public.admission_reservations), 0, 'invalid inputs create no rows');

TRUNCATE public.admission_reservations;
INSERT INTO public.admission_reservations(user_id,request_id,message_hmac_sha256,network_hmac_sha256,estimated_units,reserved_at)
SELECT 'u-rate', gen_random_uuid(), lpad(to_hex(i),64,'0'), repeat('1',64), 1, clock_timestamp() - interval '1 second'
FROM generate_series(1,20) AS g(i);
SELECT is((SELECT decision FROM public.reserve_admission('u-rate',gen_random_uuid(),repeat('f',64),repeat('2',64),1)), 'user_rate_limited', '21st user request is blocked');
SELECT is((SELECT retry_after_seconds FROM public.reserve_admission('u-rate',gen_random_uuid(),repeat('e',64),repeat('2',64),1)), 60, 'user minute retry is fixed at 60');

TRUNCATE public.admission_reservations;
INSERT INTO public.admission_reservations(user_id,request_id,message_hmac_sha256,network_hmac_sha256,estimated_units,reserved_at)
SELECT 'net-user-'||i, gen_random_uuid(), lpad(to_hex(i),64,'0'), repeat('3',64), 1, clock_timestamp() - interval '1 second'
FROM generate_series(1,60) AS g(i);
SELECT is((SELECT decision FROM public.reserve_admission('net-new',gen_random_uuid(),repeat('f',64),repeat('3',64),1)), 'network_rate_limited', '61st network request is blocked before application limit');
SELECT is((SELECT retry_after_seconds FROM public.reserve_admission('net-new-2',gen_random_uuid(),repeat('e',64),repeat('3',64),1)), 60, 'network minute retry is fixed at 60');

TRUNCATE public.admission_reservations;
INSERT INTO public.admission_reservations(user_id,request_id,message_hmac_sha256,network_hmac_sha256,estimated_units,reserved_at)
SELECT 'app-user-'||i, gen_random_uuid(), lpad(to_hex(i),64,'0'), lpad(to_hex(i+1000),64,'0'), 1, clock_timestamp() - interval '1 second'
FROM generate_series(1,25) AS g(i);
SELECT is((SELECT decision FROM public.reserve_admission('app-new',gen_random_uuid(),repeat('f',64),repeat('e',64),1)), 'application_rate_limited', '26th application request is blocked');
SELECT is((SELECT retry_after_seconds FROM public.reserve_admission('app-new-2',gen_random_uuid(),repeat('d',64),repeat('c',64),1)), 60, 'application minute retry is fixed at 60');

TRUNCATE public.admission_reservations;
INSERT INTO public.admission_reservations(user_id,request_id,message_hmac_sha256,network_hmac_sha256,estimated_units,reserved_at)
SELECT 'u-daily', gen_random_uuid(), lpad(to_hex(i),64,'0'), lpad(to_hex(i+2000),64,'0'), 1, clock_timestamp() - interval '2 minutes'
FROM generate_series(1,200) AS g(i);
SELECT is((SELECT decision FROM public.reserve_admission('u-daily',gen_random_uuid(),repeat('f',64),repeat('e',64),1)), 'user_daily_request_quota_exceeded', '201st daily request is blocked');
SELECT is((SELECT retry_after_seconds FROM public.reserve_admission('u-daily',gen_random_uuid(),repeat('d',64),repeat('c',64),1)), 86400, 'daily request retry is fixed at 86400');

TRUNCATE public.admission_reservations;
INSERT INTO public.admission_reservations(user_id,request_id,message_hmac_sha256,network_hmac_sha256,estimated_units,reserved_at)
SELECT 'u-units', gen_random_uuid(), lpad(to_hex(i),64,'0'), lpad(to_hex(i+3000),64,'0'), 5000, clock_timestamp() - interval '2 minutes'
FROM generate_series(1,50) AS g(i);
SELECT is((SELECT decision FROM public.reserve_admission('u-units',gen_random_uuid(),repeat('f',64),repeat('e',64),1)), 'user_daily_unit_quota_exceeded', 'daily units above 250000 are blocked');
SELECT is((SELECT retry_after_seconds FROM public.reserve_admission('u-units',gen_random_uuid(),repeat('d',64),repeat('c',64),1)), 86400, 'daily unit retry is fixed at 86400');

TRUNCATE public.admission_reservations;
INSERT INTO public.admission_reservations VALUES ('u-old-minute',gen_random_uuid(),repeat('a',64),repeat('b',64),1,clock_timestamp()-interval '61 seconds');
SELECT is((SELECT decision FROM public.reserve_admission('u-old-minute',gen_random_uuid(),repeat('c',64),repeat('d',64),1)), 'admitted', 'row outside 60-second window does not count');
TRUNCATE public.admission_reservations;
INSERT INTO public.admission_reservations(user_id,request_id,message_hmac_sha256,network_hmac_sha256,estimated_units,reserved_at)
SELECT 'u-old-day', gen_random_uuid(), lpad(to_hex(i),64,'0'), lpad(to_hex(i+4000),64,'0'), 6000, clock_timestamp() - interval '24 hours 1 second'
FROM generate_series(1,200) AS g(i);
SELECT is((SELECT decision FROM public.reserve_admission('u-old-day',gen_random_uuid(),repeat('f',64),repeat('e',64),1)), 'admitted', 'rows outside 24-hour window do not count');

TRUNCATE public.admission_reservations;
INSERT INTO public.admission_reservations(user_id,request_id,message_hmac_sha256,network_hmac_sha256,estimated_units,reserved_at)
SELECT 'u-boundary', gen_random_uuid(), lpad(to_hex(i),64,'0'), lpad(to_hex(i+5000),64,'0'), 6000, clock_timestamp() - interval '2 minutes'
FROM generate_series(1,41) AS g(i);
INSERT INTO public.admission_reservations VALUES ('u-boundary',gen_random_uuid(),repeat('a',64),repeat('b',64),3999,clock_timestamp()-interval '2 minutes');
SELECT is((SELECT decision FROM public.reserve_admission('u-boundary',gen_random_uuid(),repeat('c',64),repeat('d',64),1)), 'admitted', 'exactly 250000 daily units is inclusive');

SELECT * FROM finish();
ROLLBACK;
