-- Invalid admission ledger data for upgrade testing.
-- These rows violate constraints that the admission ledger migration adds
-- and should cause the migration to fail.

INSERT INTO public.admission_reservations (
    user_id,
    request_id,
    message_hmac_sha256,
    network_hmac_sha256,
    estimated_units,
    reserved_at
) VALUES (
    '',  -- empty user_id violates CHECK
    '22222222-2222-4222-a222-222222222222',
    'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
    'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd',
    100,
    NOW() - INTERVAL '1 hour'
);
