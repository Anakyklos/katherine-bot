"""Real Supabase integration tests for the transactional schema (#270).

Requires a running local Supabase instance with SUPABASE_URL,
SUPABASE_ANON_KEY and SUPABASE_SERVICE_ROLE_KEY set (database CI job only).

Covers the authorization and behavioral contract of the new server-owned
tables:

- anon / authenticated cannot reach ``turn_requests`` or ``outbox_events``
- service_role can execute the planned operations
- ``(user_id, request_id)`` uniqueness with cross-user reuse
- outbox idempotency key uniqueness with cross-user reuse
- user deletion cascades leave no orphans
- forbidden payload keys (prompt / internal fields) are rejected
- schema drift (missing migration) fails
"""

from __future__ import annotations

import os
import uuid

import pytest
from postgrest.exceptions import APIError
from supabase import create_client

INTERNAL_TABLES = ["turn_requests", "outbox_events"]


# ---------------------------------------------------------------------------
# Environment and clients
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def supabase_url():
    url = os.environ.get("SUPABASE_URL")
    assert url, "SUPABASE_URL is required for transactional schema tests"
    return url


@pytest.fixture(scope="module")
def service_role_key():
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    assert key, "SUPABASE_SERVICE_ROLE_KEY is required"
    return key


@pytest.fixture(scope="module")
def anon_key():
    key = os.environ.get("SUPABASE_ANON_KEY")
    assert key, "SUPABASE_ANON_KEY is required"
    return key


@pytest.fixture(scope="module")
def service_client(supabase_url, service_role_key):
    return create_client(supabase_url, service_role_key)


@pytest.fixture(scope="module")
def anon_client(supabase_url, anon_key):
    return create_client(supabase_url, anon_key)


@pytest.fixture(scope="module")
def auth_client(supabase_url, anon_key, service_client):
    """Create a real authenticated user and return (client, user_id)."""
    email = "transactional_schema_auth@test.com"
    password = "password123"
    client = create_client(supabase_url, anon_key)

    for user in service_client.auth.admin.list_users():
        if user.email == email:
            service_client.auth.admin.delete_user(user.id)

    client.auth.sign_up({"email": email, "password": password})
    res = client.auth.sign_in_with_password({"email": email, "password": password})
    assert res is not None and res.user is not None
    assert res.session is not None and res.session.access_token is not None
    yield client, res.user.id

    client.auth.sign_out()
    for user in service_client.auth.admin.list_users():
        if user.email == email:
            service_client.auth.admin.delete_user(user.id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def assert_denied(op, *args, **kwargs):
    """Assert an operation raises APIError with code 42501 (insufficient_privilege)."""
    with pytest.raises(APIError) as exc:
        op(*args, **kwargs).execute()
    code = getattr(exc.value, "code", None)
    assert code == "42501", (
        f"Expected 42501 (insufficient_privilege) but got code={code!r}"
    )


def _uid(label: str) -> str:
    return f"tx_{label}_{uuid.uuid4().hex[:12]}"


def _valid_turn_request_payload(user_id: str, request_id: str) -> dict:
    return {
        "user_id": user_id,
        "request_id": request_id,
        "payload_hash_sha256": "a" * 64,
        "status": "pending",
        "lease_owner": "worker-integration",
        "lease_expires_at": "2099-01-01T00:00:00Z",
        "expected_revision": 0,
    }


def _valid_outbox_payload(user_id: str, idempotency_key: str) -> dict:
    return {
        "event_type": "memory_indexed",
        "contract_version": 1,
        "user_id": user_id,
        "payload": {"ref": "turn-ref"},
        "status": "pending",
        "attempts": 0,
        "next_attempt_at": "2099-01-01T00:00:00Z",
        "idempotency_key": idempotency_key,
    }


# ---------------------------------------------------------------------------
# anon matrix
# ---------------------------------------------------------------------------


def test_anon_cannot_access_internal_tables(anon_client):
    for table in INTERNAL_TABLES:
        assert_denied(anon_client.table(table).select, "*")
        if table == "turn_requests":
            payload = _valid_turn_request_payload("anon", str(uuid.uuid4()))
        else:
            payload = _valid_outbox_payload("anon", f"idem_anon_{uuid.uuid4().hex[:12]}")
        assert_denied(anon_client.table(table).insert, payload)
        assert_denied(anon_client.table(table).update({"status": "x"}).eq, "user_id", "anon")
        assert_denied(anon_client.table(table).delete().eq, "user_id", "anon")


# ---------------------------------------------------------------------------
# authenticated matrix
# ---------------------------------------------------------------------------


def test_authenticated_cannot_access_internal_tables(auth_client, service_client):
    client, uid = auth_client
    service_client.table("profiles").upsert({"user_id": uid}).execute()
    try:
        for table in INTERNAL_TABLES:
            assert_denied(client.table(table).select, "*")
            if table == "turn_requests":
                payload = _valid_turn_request_payload(uid, str(uuid.uuid4()))
            else:
                payload = _valid_outbox_payload(uid, f"idem_{uid}")
            assert_denied(client.table(table).insert, payload)
            assert_denied(client.table(table).update({"status": "x"}).eq, "user_id", uid)
            assert_denied(client.table(table).delete().eq, "user_id", uid)
    finally:
        service_client.table("profiles").delete().eq("user_id", uid).execute()


# ---------------------------------------------------------------------------
# service_role CRUD on turn_requests
# ---------------------------------------------------------------------------


def test_service_role_turn_requests_crud(service_client):
    user_id = _uid("tr")
    service_client.table("profiles").upsert({"user_id": user_id}).execute()
    request_id = str(uuid.uuid4())
    try:
        insert_res = service_client.table("turn_requests").insert(
            _valid_turn_request_payload(user_id, request_id)
        ).execute()
        assert len(insert_res.data) == 1
        row = insert_res.data[0]
        assert row["status"] == "pending"
        internal_id = row["id"]

        select_res = service_client.table("turn_requests").select("*").eq(
            "user_id", user_id
        ).execute()
        assert len(select_res.data) == 1
        assert select_res.data[0]["request_id"] == request_id

        # Complete the request (coherent completed shape).
        update_res = service_client.table("turn_requests").update({
            "status": "completed",
            "completed_at": "2026-07-31T00:00:00Z",
            "committed_revision": 1,
            "replay_payload": {"response": "ok", "emotion_state": {"schema_version": 1}},
            "lease_owner": None,
            "lease_expires_at": None,
        }).eq("id", internal_id).execute()
        assert len(update_res.data) == 1
        assert update_res.data[0]["status"] == "completed"
        assert update_res.data[0]["committed_revision"] == 1

        delete_res = service_client.table("turn_requests").delete().eq(
            "user_id", user_id
        ).execute()
        assert len(delete_res.data) >= 1
    finally:
        service_client.table("turn_requests").delete().eq("user_id", user_id).execute()
        service_client.table("profiles").delete().eq("user_id", user_id).execute()


# ---------------------------------------------------------------------------
# service_role CRUD on outbox_events
# ---------------------------------------------------------------------------


def test_service_role_outbox_crud(service_client):
    user_id = _uid("obx")
    service_client.table("profiles").upsert({"user_id": user_id}).execute()
    try:
        insert_res = service_client.table("outbox_events").insert(
            _valid_outbox_payload(user_id, f"idem_{user_id}_1")
        ).execute()
        assert len(insert_res.data) == 1
        internal_id = insert_res.data[0]["id"]

        select_res = service_client.table("outbox_events").select("*").eq(
            "user_id", user_id
        ).execute()
        assert len(select_res.data) == 1
        assert select_res.data[0]["event_type"] == "memory_indexed"

        # Claim → processing (coherent processing shape).
        update_res = service_client.table("outbox_events").update({
            "status": "processing",
            "lease_owner": "worker-integration",
            "lease_expires_at": "2099-01-01T00:00:00Z",
            "attempts": 1,
        }).eq("id", internal_id).execute()
        assert len(update_res.data) == 1
        assert update_res.data[0]["status"] == "processing"

        delete_res = service_client.table("outbox_events").delete().eq(
            "user_id", user_id
        ).execute()
        assert len(delete_res.data) >= 1
    finally:
        service_client.table("outbox_events").delete().eq("user_id", user_id).execute()
        service_client.table("profiles").delete().eq("user_id", user_id).execute()


# ---------------------------------------------------------------------------
# Uniqueness and cross-user reuse
# ---------------------------------------------------------------------------


def test_duplicate_user_request_rejected(service_client):
    user_id = _uid("dup")
    service_client.table("profiles").upsert({"user_id": user_id}).execute()
    request_id = str(uuid.uuid4())
    try:
        service_client.table("turn_requests").insert(
            _valid_turn_request_payload(user_id, request_id)
        ).execute()
        with pytest.raises(APIError) as exc:
            service_client.table("turn_requests").insert(
                _valid_turn_request_payload(user_id, request_id)
            ).execute()
        assert getattr(exc.value, "code", None) == "23505"
    finally:
        service_client.table("turn_requests").delete().eq("user_id", user_id).execute()
        service_client.table("profiles").delete().eq("user_id", user_id).execute()


def test_same_request_id_allowed_for_different_users(service_client):
    user_a = _uid("ra")
    user_b = _uid("rb")
    service_client.table("profiles").upsert([
        {"user_id": user_a},
        {"user_id": user_b},
    ]).execute()
    request_id = str(uuid.uuid4())
    try:
        service_client.table("turn_requests").insert(
            _valid_turn_request_payload(user_a, request_id)
        ).execute()
        service_client.table("turn_requests").insert(
            _valid_turn_request_payload(user_b, request_id)
        ).execute()
        res = service_client.table("turn_requests").select("user_id").eq(
            "request_id", request_id
        ).execute()
        assert len(res.data) == 2
    finally:
        for uid in (user_a, user_b):
            service_client.table("turn_requests").delete().eq("user_id", uid).execute()
            service_client.table("profiles").delete().eq("user_id", uid).execute()


def test_duplicate_outbox_idempotency_rejected(service_client):
    user_id = _uid("idem")
    service_client.table("profiles").upsert({"user_id": user_id}).execute()
    try:
        service_client.table("outbox_events").insert(
            _valid_outbox_payload(user_id, "idem-key-1")
        ).execute()
        with pytest.raises(APIError) as exc:
            service_client.table("outbox_events").insert(
                _valid_outbox_payload(user_id, "idem-key-1")
            ).execute()
        assert getattr(exc.value, "code", None) == "23505"
    finally:
        service_client.table("outbox_events").delete().eq("user_id", user_id).execute()
        service_client.table("profiles").delete().eq("user_id", user_id).execute()


def test_same_outbox_idempotency_allowed_across_users(service_client):
    user_a = _uid("ia")
    user_b = _uid("ib")
    service_client.table("profiles").upsert([
        {"user_id": user_a},
        {"user_id": user_b},
    ]).execute()
    try:
        service_client.table("outbox_events").insert(
            _valid_outbox_payload(user_a, "shared-key")
        ).execute()
        service_client.table("outbox_events").insert(
            _valid_outbox_payload(user_b, "shared-key")
        ).execute()
        res = service_client.table("outbox_events").select("user_id").eq(
            "idempotency_key", "shared-key"
        ).execute()
        assert len(res.data) == 2
    finally:
        for uid in (user_a, user_b):
            service_client.table("outbox_events").delete().eq("user_id", uid).execute()
            service_client.table("profiles").delete().eq("user_id", uid).execute()


# ---------------------------------------------------------------------------
# Forbidden payload keys (prompt / internal fields)
# ---------------------------------------------------------------------------


def test_turn_request_replay_payload_forbids_prompt(service_client):
    user_id = _uid("rp")
    service_client.table("profiles").upsert({"user_id": user_id}).execute()
    try:
        payload = {
            "user_id": user_id,
            "request_id": str(uuid.uuid4()),
            "payload_hash_sha256": "b" * 64,
            "status": "completed",
            "completed_at": "2026-07-31T00:00:00Z",
            "committed_revision": 1,
            "replay_payload": {"prompt": "hidden", "response": "x"},
        }
        with pytest.raises(APIError) as exc:
            service_client.table("turn_requests").insert(payload).execute()
        assert getattr(exc.value, "code", None) == "23514"
    finally:
        service_client.table("turn_requests").delete().eq("user_id", user_id).execute()
        service_client.table("profiles").delete().eq("user_id", user_id).execute()


def test_outbox_payload_forbids_prompt(service_client):
    user_id = _uid("op")
    service_client.table("profiles").upsert({"user_id": user_id}).execute()
    try:
        payload = _valid_outbox_payload(user_id, f"idem_{user_id}")
        payload["payload"] = {"prompt": "hidden"}
        with pytest.raises(APIError) as exc:
            service_client.table("outbox_events").insert(payload).execute()
        assert getattr(exc.value, "code", None) == "23514"
    finally:
        service_client.table("outbox_events").delete().eq("user_id", user_id).execute()
        service_client.table("profiles").delete().eq("user_id", user_id).execute()


def test_outbox_payload_forbids_nested_internal(service_client):
    """Forbidden keys must be rejected at any depth, not only top level."""
    user_id = _uid("on")
    service_client.table("profiles").upsert({"user_id": user_id}).execute()
    try:
        payload = _valid_outbox_payload(user_id, f"idem_{user_id}")
        payload["payload"] = {"ref": {"system_prompt": "hidden"}}
        with pytest.raises(APIError) as exc:
            service_client.table("outbox_events").insert(payload).execute()
        assert getattr(exc.value, "code", None) == "23514"
    finally:
        service_client.table("outbox_events").delete().eq("user_id", user_id).execute()
        service_client.table("profiles").delete().eq("user_id", user_id).execute()


def test_outbox_payload_forbids_message_content(service_client):
    """The outbox must never store message content, even under an allowed key."""
    user_id = _uid("om")
    service_client.table("profiles").upsert({"user_id": user_id}).execute()
    try:
        payload = _valid_outbox_payload(user_id, f"idem_{user_id}")
        payload["payload"] = {"ref": "turn-1", "message": "conteudo sensivel"}
        with pytest.raises(APIError) as exc:
            service_client.table("outbox_events").insert(payload).execute()
        assert getattr(exc.value, "code", None) == "23514"
    finally:
        service_client.table("outbox_events").delete().eq("user_id", user_id).execute()
        service_client.table("profiles").delete().eq("user_id", user_id).execute()


def test_outbox_payload_forbids_unknown_top_level_key(service_client):
    """The explicit allowlist rejects unknown top-level keys."""
    user_id = _uid("ou")
    service_client.table("profiles").upsert({"user_id": user_id}).execute()
    try:
        payload = _valid_outbox_payload(user_id, f"idem_{user_id}")
        payload["payload"] = {"ref": "turn-1", "unknown_key": 1}
        with pytest.raises(APIError) as exc:
            service_client.table("outbox_events").insert(payload).execute()
        assert getattr(exc.value, "code", None) == "23514"
    finally:
        service_client.table("outbox_events").delete().eq("user_id", user_id).execute()
        service_client.table("profiles").delete().eq("user_id", user_id).execute()


# ---------------------------------------------------------------------------
# Cross-user isolation (composite FKs)
# ---------------------------------------------------------------------------


def test_cross_user_message_reference_rejected(service_client):
    """A request for user A must not be able to reference a chat_logs
    message owned by user B (composite FK on (user_id, message_id))."""
    user_a = _uid("msg_a")
    user_b = _uid("msg_b")
    service_client.table("profiles").upsert([
        {"user_id": user_a},
        {"user_id": user_b},
    ]).execute()
    try:
        chat = service_client.table("chat_logs").insert({
            "user_id": user_a,
            "role": "user",
            "content": "message owned by user A",
        }).execute()
        message_id = chat.data[0]["id"]

        payload = _valid_turn_request_payload(user_b, str(uuid.uuid4()))
        payload["user_message_chat_log_id"] = message_id
        with pytest.raises(APIError) as exc:
            service_client.table("turn_requests").insert(payload).execute()
        assert getattr(exc.value, "code", None) == "23503"
    finally:
        service_client.table("chat_logs").delete().eq("user_id", user_a).execute()
        for uid in (user_a, user_b):
            service_client.table("turn_requests").delete().eq("user_id", uid).execute()
            service_client.table("profiles").delete().eq("user_id", uid).execute()


def test_cross_user_turn_request_reference_rejected(service_client):
    """An outbox event for user B must not be able to reference a
    turn_request owned by user A (composite FK on (user_id, turn_request_id))."""
    user_a = _uid("req_a")
    user_b = _uid("req_b")
    service_client.table("profiles").upsert([
        {"user_id": user_a},
        {"user_id": user_b},
    ]).execute()
    try:
        req = service_client.table("turn_requests").insert(
            _valid_turn_request_payload(user_a, str(uuid.uuid4()))
        ).execute()
        request_id = req.data[0]["id"]

        payload = _valid_outbox_payload(user_b, f"idem_{user_b}")
        payload["turn_request_id"] = request_id
        with pytest.raises(APIError) as exc:
            service_client.table("outbox_events").insert(payload).execute()
        assert getattr(exc.value, "code", None) == "23503"
    finally:
        for uid in (user_a, user_b):
            service_client.table("turn_requests").delete().eq("user_id", uid).execute()
            service_client.table("outbox_events").delete().eq("user_id", uid).execute()
            service_client.table("profiles").delete().eq("user_id", uid).execute()


# ---------------------------------------------------------------------------
# Cascade: user deletion leaves no orphans
# ---------------------------------------------------------------------------


def test_user_delete_cascades_internal_rows(service_client):
    user_id = _uid("cascade")
    service_client.table("profiles").upsert({"user_id": user_id}).execute()
    try:
        service_client.table("turn_requests").insert(
            _valid_turn_request_payload(user_id, str(uuid.uuid4()))
        ).execute()
        service_client.table("outbox_events").insert(
            _valid_outbox_payload(user_id, f"idem_{user_id}")
        ).execute()

        # Delete the user's profile; internal rows must cascade away.
        service_client.table("profiles").delete().eq("user_id", user_id).execute()

        res = service_client.table("turn_requests").select("user_id").eq(
            "user_id", user_id
        ).execute()
        assert len(res.data) == 0
        res = service_client.table("outbox_events").select("user_id").eq(
            "user_id", user_id
        ).execute()
        assert len(res.data) == 0
    finally:
        service_client.table("turn_requests").delete().eq("user_id", user_id).execute()
        service_client.table("outbox_events").delete().eq("user_id", user_id).execute()
        service_client.table("profiles").delete().eq("user_id", user_id).execute()



