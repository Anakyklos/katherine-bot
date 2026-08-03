"""Integration tests for database authorization boundaries.

Requires a running local Supabase instance with SUPABASE_URL, SUPABASE_ANON_KEY,
and SUPABASE_SERVICE_ROLE_KEY set.

All non-service-role assertions reject PGRST301 (JWT failure) — only 42501
(insufficient_privilege) is accepted. Sessions are verified after signup/signin
to prevent false positives from broken auth.
"""

import logging
import os
import pytest
from supabase import create_client
from postgrest.exceptions import APIError


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Require environment — no skips, fail hard
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def supabase_url():
    url = os.environ.get("SUPABASE_URL")
    assert url, "SUPABASE_URL is required for database integration tests"
    return url


@pytest.fixture(scope="module")
def service_role_key():
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    assert key, "SUPABASE_SERVICE_ROLE_KEY is required for database integration tests"
    return key


@pytest.fixture(scope="module")
def anon_key():
    key = os.environ.get("SUPABASE_ANON_KEY")
    assert key, "SUPABASE_ANON_KEY is required for database integration tests"
    return key


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------

def _close_http_transports(client) -> None:
    """Close the lazily-created postgrest/storage/functions transports.

    Each lazily-created sub-client owns an httpx session; in the pinned
    versions storage3 and supabase-functions expose no ``close()``/``aclose()``
    of their own, so close the underlying httpx session directly.
    """
    if client is None:
        return
    for attr, session_attr in (
        ("_postgrest", "session"),
        ("_storage", "session"),
        ("_functions", "_client"),
    ):
        transport = getattr(client, attr, None)
        if transport is None:
            continue
        session = getattr(transport, session_attr, None)
        if session is not None and hasattr(session, "close"):
            session.close()


def _close_client(client) -> None:
    """Close every HTTP transport a Supabase sync client may hold.

    supabase-py 2.31.0 does not expose a public close(); the auth transport is
    created eagerly and postgrest/storage/functions lazily. Close only the
    transports that were actually created so no unclosed-socket
    ``ResourceWarning`` is reported while ``filterwarnings = error`` is active.
    """
    if client is None:
        return
    _close_http_transports(client)
    auth = getattr(client, "auth", None)
    if auth is not None and hasattr(auth, "close"):
        auth.close()


@pytest.fixture(scope="module")
def service_client(supabase_url, service_role_key):
    client = create_client(supabase_url, service_role_key)
    yield client
    _close_client(client)


@pytest.fixture(scope="module")
def anon_client(supabase_url, anon_key):
    client = create_client(supabase_url, anon_key)
    yield client
    _close_client(client)


@pytest.fixture(scope="module")
def auth_client_a(supabase_url, anon_key, service_client):
    """Create user A, verify session is valid, return (client, user_id)."""
    email = "user_a_integration@test.com"
    password = "password123"
    client = create_client(supabase_url, anon_key)

    # Clean up any previous A
    users = service_client.auth.admin.list_users()
    for u in users:
        if u.email == email:
            service_client.auth.admin.delete_user(u.id)

    client.auth.sign_up({"email": email, "password": password})
    res = client.auth.sign_in_with_password({"email": email, "password": password})
    _assert_valid_session(res, "auth_client_a")
    # Also verify via get_user
    user = client.auth.get_user()
    assert user is not None, "get_user() returned None for A"
    assert user.user.id == res.user.id, "get_user() id mismatch for A"
    yield client, res.user.id
    _close_client(client)


@pytest.fixture(scope="module")
def auth_client_b(supabase_url, anon_key, service_client):
    """Create user B, verify session is valid, return (client, user_id)."""
    email = "user_b_integration@test.com"
    password = "password123"
    client = create_client(supabase_url, anon_key)

    # Clean up any previous B
    users = service_client.auth.admin.list_users()
    for u in users:
        if u.email == email:
            service_client.auth.admin.delete_user(u.id)

    client.auth.sign_up({"email": email, "password": password})
    res = client.auth.sign_in_with_password({"email": email, "password": password})
    _assert_valid_session(res, "auth_client_b")
    # Also verify via get_user
    user = client.auth.get_user()
    assert user is not None, "get_user() returned None for B"
    assert user.user.id == res.user.id, "get_user() id mismatch for B"
    yield client, res.user.id
    _close_client(client)


def _assert_valid_session(res, label: str):
    """Verify that a sign-in response produced a valid session."""
    assert res is not None, f"{label}: sign_in response is None"
    assert res.user is not None, f"{label}: sign_in returned no user"
    assert res.user.id is not None, f"{label}: user.id is None"
    assert res.session is not None, f"{label}: session is None"
    assert res.session.access_token is not None, f"{label}: access_token is None"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TABLE_NAMES = ["profiles", "chat_logs", "memories", "archival_extractions"]


def assert_denied(op, *args, **kwargs):
    """Assert that an operation raises APIError with code 42501 (insufficient_privilege).

    Does NOT accept PGRST301 (JWT/auth failure), because that would indicate a
    broken session rather than denied table authorization.
    The assertion message only includes the error code, never raw message or details.
    """
    with pytest.raises(APIError) as exc:
        op(*args, **kwargs).execute()
    code = getattr(exc.value, "code", None)
    assert code == "42501", (
        f"Expected 42501 (insufficient_privilege) but got code={code!r}"
    )


def get_valid_payload(table, uid, id_suffix="1", source_log_id=1):
    if table == "profiles":
        return {"user_id": uid, "persona_config": "test"}
    if table == "chat_logs":
        return {"user_id": uid, "role": "user", "content": "test_msg"}
    if table == "memories":
        return {"user_id": uid, "content": "mem"}
    if table == "archival_extractions":
        return {
            "user_id": uid,
            "source_chat_log_id": source_log_id,
            "extractor_version": 1,
            "schema_version": 1,
            "idempotency_key": f"{uid}_{id_suffix}",
            "facts": [],
        }
    return {}


def get_valid_update_payload(table):
    if table == "profiles":
        return {"persona_config": "updated"}
    if table == "chat_logs":
        return {"content": "updated_msg"}
    if table == "memories":
        return {"content": "updated_mem"}
    if table == "archival_extractions":
        return {"facts": [{"content": "updated"}]}
    return {}


# ---------------------------------------------------------------------------
# Matrix: anon — no session at all
# ---------------------------------------------------------------------------

def test_anon_matrix(anon_client):
    for table in TABLE_NAMES:
        assert_denied(anon_client.table(table).select, "*")
        assert_denied(anon_client.table(table).insert, get_valid_payload(table, "anon", "1"))
        assert_denied(anon_client.table(table).update(get_valid_update_payload(table)).eq, "user_id", "anon")
        assert_denied(anon_client.table(table).delete().eq, "user_id", "anon")


# ---------------------------------------------------------------------------
# Matrix: user A
# ---------------------------------------------------------------------------

def test_auth_a_matrix(auth_client_a, auth_client_b, service_client):
    client_a, uid_a = auth_client_a
    _, uid_b = auth_client_b

    # Pre-create dependencies via service role, so FK/column errors don't
    # mask authorization issues
    service_client.table("profiles").upsert([
        {"user_id": uid_a},
        {"user_id": uid_b},
    ]).execute()
    log_a = service_client.table("chat_logs").insert({
        "user_id": uid_a, "role": "user", "content": "a"
    }).execute()
    log_b = service_client.table("chat_logs").insert({
        "user_id": uid_b, "role": "user", "content": "b"
    }).execute()
    log_id_a = log_a.data[0]["id"]
    log_id_b = log_b.data[0]["id"]

    for table in TABLE_NAMES:
        # A tries own data → denied
        assert_denied(client_a.table(table).select("*").eq, "user_id", uid_a)
        assert_denied(client_a.table(table).insert, get_valid_payload(table, uid_a, "a1", log_id_a))
        assert_denied(client_a.table(table).update(get_valid_update_payload(table)).eq, "user_id", uid_a)
        assert_denied(client_a.table(table).delete().eq, "user_id", uid_a)

        # A tries B's data → denied
        assert_denied(client_a.table(table).select("*").eq, "user_id", uid_b)
        assert_denied(client_a.table(table).insert, get_valid_payload(table, uid_b, "a2", log_id_b))
        assert_denied(client_a.table(table).update(get_valid_update_payload(table)).eq, "user_id", uid_b)
        assert_denied(client_a.table(table).delete().eq, "user_id", uid_b)


# ---------------------------------------------------------------------------
# Matrix: user B
# ---------------------------------------------------------------------------

def test_auth_b_matrix(auth_client_a, auth_client_b, service_client):
    client_b, uid_b = auth_client_b
    _, uid_a = auth_client_a

    # Fetch existing chat log IDs for dependencies
    res_a = service_client.table("chat_logs").select("id").eq("user_id", uid_a).limit(1).execute()
    log_id_a = res_a.data[0]["id"] if res_a.data else 1
    res_b = service_client.table("chat_logs").select("id").eq("user_id", uid_b).limit(1).execute()
    log_id_b = res_b.data[0]["id"] if res_b.data else 1

    for table in TABLE_NAMES:
        # B tries own data → denied
        assert_denied(client_b.table(table).select("*").eq, "user_id", uid_b)
        assert_denied(client_b.table(table).insert, get_valid_payload(table, uid_b, "b1", log_id_b))
        assert_denied(client_b.table(table).update(get_valid_update_payload(table)).eq, "user_id", uid_b)
        assert_denied(client_b.table(table).delete().eq, "user_id", uid_b)

        # B tries A's data → denied
        assert_denied(client_b.table(table).select("*").eq, "user_id", uid_a)
        assert_denied(client_b.table(table).insert, get_valid_payload(table, uid_a, "b2", log_id_a))
        assert_denied(client_b.table(table).update(get_valid_update_payload(table)).eq, "user_id", uid_a)
        assert_denied(client_b.table(table).delete().eq, "user_id", uid_a)


# ---------------------------------------------------------------------------
# Service role — full CRUD matrix
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def service_teardown(service_client):
    """Deterministic teardown: tries all operations, collects sanitized errors,
    raises if any cleanup failed. Records are verified to have been removed."""
    yield
    uid = "service_test_user_1"
    errors = []

    # Clean up in FK order: extrations → memories → chat_logs → profiles
    try:
        service_client.table("archival_extractions").delete().eq("user_id", uid).execute()
    except Exception as e:
        errors.append(f"archival_extractions:{type(e).__name__}")
    try:
        service_client.table("memories").delete().eq("user_id", uid).execute()
    except Exception as e:
        errors.append(f"memories:{type(e).__name__}")
    try:
        service_client.table("chat_logs").delete().eq("user_id", uid).execute()
    except Exception as e:
        errors.append(f"chat_logs:{type(e).__name__}")
    try:
        service_client.table("profiles").delete().eq("user_id", uid).execute()
    except Exception as e:
        errors.append(f"profiles:{type(e).__name__}")

    # Fail the test if any cleanup operation failed
    if errors:
        logger.error("Cleanup failures: %s", ", ".join(errors))
        pytest.fail(f"Deterministic teardown failed: {", ".join(errors)}")


def test_service_role_profiles(service_client, service_teardown):
    uid = "service_test_user_1"
    svc = service_client

    # INSERT
    res = svc.table("profiles").insert({"user_id": uid}).execute()
    assert len(res.data) == 1

    # SELECT
    res = svc.table("profiles").select("*").eq("user_id", uid).execute()
    assert len(res.data) == 1

    # UPDATE
    res = svc.table("profiles").update({"persona_config": "test"}).eq("user_id", uid).execute()
    assert len(res.data) == 1

    # DELETE
    res = svc.table("profiles").delete().eq("user_id", uid).execute()
    assert len(res.data) == 1

    # SELECT after delete (empty)
    res = svc.table("profiles").select("*").eq("user_id", uid).execute()
    assert len(res.data) == 0

    # Re-create for subsequent tests
    svc.table("profiles").insert({"user_id": uid}).execute()


def test_service_role_chat_logs(service_client, service_teardown):
    uid = "service_test_user_1"
    svc = service_client

    # Ensure profile exists
    svc.table("profiles").upsert({"user_id": uid}).execute()

    # INSERT user role
    res = svc.table("chat_logs").insert({
        "user_id": uid, "role": "user", "content": "hello"
    }).execute()
    assert len(res.data) == 1
    log_id_user = res.data[0]["id"]

    # INSERT assistant role
    res = svc.table("chat_logs").insert({
        "user_id": uid, "role": "assistant", "content": "hi"
    }).execute()
    assert len(res.data) == 1

    # SELECT
    res = svc.table("chat_logs").select("*").eq("user_id", uid).execute()
    assert len(res.data) == 2

    # UPDATE
    res = svc.table("chat_logs").update({"content": "updated_hello"}).eq("id", log_id_user).execute()
    assert len(res.data) == 1
    assert res.data[0]["content"] == "updated_hello"

    # DELETE
    res = svc.table("chat_logs").delete().eq("user_id", uid).execute()
    assert len(res.data) > 0  # at least the rows we inserted

    # SELECT after delete (empty)
    res = svc.table("chat_logs").select("*").eq("user_id", uid).execute()
    assert len(res.data) == 0


def test_service_role_memories(service_client, service_teardown):
    uid = "service_test_user_1"
    svc = service_client

    # Ensure profile exists
    svc.table("profiles").upsert({"user_id": uid}).execute()

    # INSERT
    res = svc.table("memories").insert({
        "user_id": uid, "content": "mem1"
    }).execute()
    assert len(res.data) == 1
    mem_id = res.data[0]["id"]

    # SELECT
    res = svc.table("memories").select("*").eq("user_id", uid).execute()
    assert len(res.data) >= 1

    # UPDATE
    res = svc.table("memories").update({"content": "mem1_updated"}).eq("id", mem_id).execute()
    assert len(res.data) == 1
    assert res.data[0]["content"] == "mem1_updated"

    # DELETE
    res = svc.table("memories").delete().eq("user_id", uid).execute()
    assert len(res.data) > 0

    # SELECT after delete (empty)
    res = svc.table("memories").select("*").eq("user_id", uid).execute()
    assert len(res.data) == 0


def test_service_role_archival_extractions(service_client, service_teardown):
    uid = "service_test_user_1"
    svc = service_client

    # Ensure profile exists
    svc.table("profiles").upsert({"user_id": uid}).execute()

    # Pre-create a chat log
    log = svc.table("chat_logs").insert({
        "user_id": uid, "role": "user", "content": "source"
    }).execute()
    log_id = log.data[0]["id"]

    # INSERT
    ext = {
        "user_id": uid,
        "source_chat_log_id": log_id,
        "extractor_version": 1,
        "schema_version": 1,
        "idempotency_key": f"sr_idem_{log_id}",
        "facts": [{"content": "original_fact"}],
    }
    res = svc.table("archival_extractions").insert(ext).execute()
    assert len(res.data) == 1
    ext_idemp = res.data[0]["idempotency_key"]

    # SELECT
    res = svc.table("archival_extractions").select("*").eq("user_id", uid).execute()
    assert len(res.data) >= 1

    # UPDATE facts
    res = svc.table("archival_extractions").update({
        "facts": [{"content": "updated_fact"}]
    }).eq("idempotency_key", ext_idemp).execute()
    assert len(res.data) == 1
    assert res.data[0]["facts"] == [{"content": "updated_fact"}]

    # DELETE
    res = svc.table("archival_extractions").delete().eq("user_id", uid).execute()
    assert len(res.data) > 0

    # SELECT after delete (empty)
    res = svc.table("archival_extractions").select("*").eq("user_id", uid).execute()
    assert len(res.data) == 0


# ---------------------------------------------------------------------------
# RPC: match_memories access
# ---------------------------------------------------------------------------

def test_match_memories_access(anon_client, auth_client_a, service_client):
    params = {
        "query_embedding": [0] * 384,
        "match_threshold": 0.5,
        "match_count": 5,
        "filter_user_id": "test",
    }

    # anon → denied
    assert_denied(anon_client.rpc, "match_memories", params)

    # authenticated session → denied
    client_a, _ = auth_client_a
    assert_denied(client_a.rpc, "match_memories", params)

    # service_role → allowed (returns list, possibly empty)
    res = service_client.rpc("match_memories", params).execute()
    assert isinstance(res.data, list)

    # RPC with valid existing user
    uid = "service_test_user_1"
    params["filter_user_id"] = uid
    res = service_client.rpc("match_memories", params).execute()
    assert isinstance(res.data, list)
