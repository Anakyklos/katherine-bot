-- Valid admission ledger data for upgrade testing.
-- These rows should pass admission ledger migration constraints
-- and be queryable via the RPC after migration.

INSERT INTO public.admission_reservations (
    user_id,
    request_id,
    message_hmac_sha256,
    network_hmac_sha256,
    estimated_units,
    reserved_at
) VALUES (
    'admission_user_valid',
    '11111111-1111-4111-a111-111111111111',
    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
    100,
    NOW() - INTERVAL '2 hours'
);
