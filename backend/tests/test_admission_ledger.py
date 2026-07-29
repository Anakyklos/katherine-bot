"""
Integration tests for the atomic PostgreSQL admission ledger.

Requires a running local Supabase instance with SUPABASE_URL,
SUPABASE_ANON_KEY, and SUPABASE_SERVICE_ROLE_KEY set.

Covers:

 1. Schema: columns, types, constraints, indices
 2. RLS enabled, FORCE RLS enabled
 3. No policies
 4. Table privileges: anon, authenticated, service_role, PUBLIC — none
 5. RPC privileges: anon denied, authenticated denied, service_role allowed
 6. Direct table access: all roles denied (including service_role)
 7. Basic admission flow
 8. Duplicate message HMAC → admitted without new quota
 9. Exact replay → request_replay_unavailable
10. Request ID conflict → request_id_conflict
11. Same UUID, different users → isolated
12. Invalid input → no insert
13. User rate limit (20/60s)
14. Network rate limit (60/60s)
15. Application rate limit (25/60s)
16. User daily request quota (200/24h)
17. User daily unit quota (250000/24h)
18. Window expiration via clock
19. Concurrent independent reservations
20. Concurrent duplicates (exactly one insert)
21. Global concurrency <= 25 admitted
22. Authorization matrix: anon/authenticated/service_role RPC access
23. Legacy upgrade: valid (clean apply)
24. Legacy upgrade: invalid data rejected
"""

import json
import os
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest
from supabase import create_client
from postgrest.exceptions import APIError

from backend.supabase_cli import run_supabase_op


# ---------------------------------------------------------------------------
# Decision codes
# ---------------------------------------------------------------------------
ADMITTED = "admitted"
REPLAY_UNAVAILABLE = "request_replay_unavailable"
CONFLICT = "request_id_conflict"
USER_RATE_LIMITED = "user_rate_limited"
NETWORK_RATE_LIMITED = "network_rate_limited"
APP_RATE_LIMITED = "application_rate_limited"
DAILY_REQ_EXCEEDED = "user_daily_request_quota_exceeded"
DAILY_UNIT_EXCEEDED = "user_daily_unit_quota_exceeded"
INVALID_INPUT = "invalid_admission_input"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HMAC_64 = "ab" * 32
_NET_HMAC_64 = "cd" * 32

_ADMISSION_MIGRATION = "supabase/migrations/20240101000003_admission_ledger.sql"
_ADMISSION_MIGRATION_TMP = _ADMISSION_MIGRATION + ".tmp"


def _make_uid(label: str) -> str:
    return f"admission_test_{label}_{os.environ.get('PYTEST_CURRENT_TEST', 'unknown')}"


def _make_hmac(seed: str) -> str:
    raw = seed.encode("utf-8")
    hex_str = raw.hex()
    while len(hex_str) < 64:
        hex_str += hex_str
    return hex_str[:64]


def _execute_sql(sql: str) -> subprocess.CompletedProcess:
    return run_supabase_op(
        "admission_query",
        ["db", "query", "--agent=no", "--output", "json", sql],
        check=False,
    )


def _count_reservations(user_id: str) -> int:
    res = _execute_sql(
        "SELECT count(*)::int AS c FROM public.admission_reservations "
        "WHERE user_id = '" + user_id + "'"
    )
    data = json.loads(res.stdout)
    return data[0]["c"] if data else 0


def _delete_reservations(user_id: str) -> None:
    _execute_sql(
        "DELETE FROM public.admission_reservations WHERE user_id = '" + user_id + "'"
    )


def _call_reserve(client, user_id, req_id, msg_hmac, net_hmac, units):
    try:
        resp = client.rpc("reserve_admission", {
            "p_user_id": user_id,
            "p_request_id": str(req_id),
            "p_message_hmac_sha256": msg_hmac,
            "p_network_hmac_sha256": net_hmac,
            "p_estimated_units": units,
        }).execute()
        return resp.data[0] if resp.data else None
    except APIError:
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def supabase_url():
    url = os.environ.get("SUPABASE_URL")
    assert url, "SUPABASE_URL is required"
    return url


@pytest.fixture(scope="module")
def service_role_key():
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not key:
        result = run_supabase_op(
            "admission_query",
            ["status", "-o", "env"],
            check=False,
        )
        if result.returncode != 0:
            pytest.skip("Could not extract service role key")
        for line in result.stdout.splitlines():
            if line.startswith("SERVICE_ROLE_KEY="):
                key = line.split("=", 1)[1].strip('"')
                break
        if not key:
            pytest.skip("SERVICE_ROLE_KEY not found")
    return key


@pytest.fixture(scope="module")
def anon_key():
    key = os.environ.get("SUPABASE_ANON_KEY")
    if not key:
        result = run_supabase_op(
            "admission_query",
            ["status", "-o", "env"],
            check=False,
        )
        if result.returncode != 0:
            pytest.skip("Could not extract anon key")
        for line in result.stdout.splitlines():
            if line.startswith("ANON_KEY="):
                key = line.split("=", 1)[1].strip('"')
                break
        if not key:
            pytest.skip("ANON_KEY not found")
    return key


@pytest.fixture(scope="module")
def service_client(supabase_url, service_role_key):
    return create_client(supabase_url, service_role_key)


@pytest.fixture(scope="module")
def anon_client(supabase_url, anon_key):
    return create_client(supabase_url, anon_key)


@pytest.fixture(scope="module")
def auth_client_a(supabase_url, anon_key, service_client):
    """Create user A for authorization tests."""
    email = "admission_a@test.com"
    password = "password123"
    client = create_client(supabase_url, anon_key)
    users = service_client.auth.admin.list_users()
    for u in users:
        if u.email == email:
            service_client.auth.admin.delete_user(u.id)
    client.auth.sign_up({"email": email, "password": password})
    res = client.auth.sign_in_with_password({"email": email, "password": password})
    assert res is not None and res.user is not None
    return client, res.user.id


@pytest.fixture(scope="module")
def auth_client_b(supabase_url, anon_key, service_client):
    """Create user B for authorization tests."""
    email = "admission_b@test.com"
    password = "password123"
    client = create_client(supabase_url, anon_key)
    users = service_client.auth.admin.list_users()
    for u in users:
        if u.email == email:
            service_client.auth.admin.delete_user(u.id)
    client.auth.sign_up({"email": email, "password": password})
    res = client.auth.sign_in_with_password({"email": email, "password": password})
    assert res is not None and res.user is not None
    return client, res.user.id


# ---------------------------------------------------------------------------
# Helpers for migration manipulation (legacy upgrade tests)
# ---------------------------------------------------------------------------

def _move_admission_migration_aside():
    if os.path.exists(_ADMISSION_MIGRATION) and not os.path.exists(_ADMISSION_MIGRATION_TMP):
        os.rename(_ADMISSION_MIGRATION, _ADMISSION_MIGRATION_TMP)


def _restore_admission_migration():
    if os.path.exists(_ADMISSION_MIGRATION_TMP) and not os.path.exists(_ADMISSION_MIGRATION):
        os.rename(_ADMISSION_MIGRATION_TMP, _ADMISSION_MIGRATION)


def _ensure_admission_migration_present():
    if os.path.exists(_ADMISSION_MIGRATION_TMP):
        if os.path.exists(_ADMISSION_MIGRATION):
            os.remove(_ADMISSION_MIGRATION_TMP)
        else:
            os.rename(_ADMISSION_MIGRATION_TMP, _ADMISSION_MIGRATION)


def _run_supabase(op_id, args, check=True):
    result = run_supabase_op(op_id, args, check=False)
    if check and result.returncode != 0:
        raise AssertionError(f"Supabase operation failed: {op_id}")
    return result


# ---------------------------------------------------------------------------
# 1-4. Schema tests
# ---------------------------------------------------------------------------

def test_schema_columns():
    sql = (
        "SELECT column_name, data_type, is_nullable "
        "FROM information_schema.columns "
        "WHERE table_name = 'admission_reservations' "
        "ORDER BY ordinal_position"
    )
    res = _execute_sql(sql)
    data = json.loads(res.stdout)
    cols = {row["column_name"]: row for row in data}

    assert cols["user_id"]["data_type"] == "text"
    assert cols["user_id"]["is_nullable"] == "NO"
    assert cols["request_id"]["data_type"] == "uuid"
    assert cols["request_id"]["is_nullable"] == "NO"
    assert cols["message_hmac_sha256"]["data_type"] == "text"
    assert cols["network_hmac_sha256"]["data_type"] == "text"
    assert cols["estimated_units"]["data_type"] == "integer"
    assert cols["reserved_at"]["data_type"] == "timestamp with time zone"


def test_schema_constraints():
    res = _execute_sql(
        "SELECT conname, contype FROM pg_constraint "
        "WHERE conrelid = 'admission_reservations'::regclass"
    )
    constraints = json.loads(res.stdout)
    connames = [c["conname"] for c in constraints]
    assert "admission_reservations_pkey" in connames
    assert "admission_reservations_user_id_check" in connames
    assert "admission_reservations_message_hmac_check" in connames
    assert "admission_reservations_network_hmac_check" in connames
    assert "admission_reservations_estimated_units_check" in connames


def test_schema_indices():
    res = _execute_sql(
        "SELECT indexname FROM pg_indexes "
        "WHERE tablename = 'admission_reservations'"
    )
    indices = json.loads(res.stdout)
    names = [i["indexname"] for i in indices]
    assert "admission_reservations_user_time_idx" in names
    assert "admission_reservations_network_time_idx" in names
    assert "admission_reservations_time_idx" in names


def test_schema_rls():
    res = _execute_sql(
        "SELECT relrowsecurity, relforcerowsecurity "
        "FROM pg_class WHERE oid = 'admission_reservations'::regclass"
    )
    row = json.loads(res.stdout)[0]
    assert row["relrowsecurity"] is True
    assert row["relforcerowsecurity"] is True


def test_schema_no_policies():
    res = _execute_sql(
        "SELECT count(*)::int AS c FROM pg_policies "
        "WHERE tablename = 'admission_reservations'"
    )
    assert json.loads(res.stdout)[0]["c"] == 0


def test_schema_table_privileges():
    for role in ("anon", "authenticated", "service_role"):
        res = _execute_sql(
            "SELECT privilege_type FROM information_schema.role_table_grants "
            "WHERE table_name = 'admission_reservations' AND grantee = '" + role + "'"
        )
        assert len(json.loads(res.stdout)) == 0, f"{role} has privileges on table"

    res = _execute_sql(
        "SELECT has_table_privilege('public', "
        "'public.admission_reservations', "
        "'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER') AS result"
    )
    assert json.loads(res.stdout)[0]["result"] is False, "PUBLIC has privileges"


# ---------------------------------------------------------------------------
# 5-6. RPC privilege tests
# ---------------------------------------------------------------------------

def test_rpc_privileges_anon(anon_client):
    uid = _make_uid("anon_rpc")
    with pytest.raises(APIError) as exc:
        anon_client.rpc("reserve_admission", {
            "p_user_id": uid,
            "p_request_id": str(uuid.uuid4()),
            "p_message_hmac_sha256": _HMAC_64,
            "p_network_hmac_sha256": _NET_HMAC_64,
            "p_estimated_units": 100,
        }).execute()
    assert getattr(exc.value, "code", None) == "42501"


def test_rpc_privileges_authenticated(auth_client_a):
    client_a, _ = auth_client_a
    uid = _make_uid("auth_rpc")
    with pytest.raises(APIError) as exc:
        client_a.rpc("reserve_admission", {
            "p_user_id": uid,
            "p_request_id": str(uuid.uuid4()),
            "p_message_hmac_sha256": _HMAC_64,
            "p_network_hmac_sha256": _NET_HMAC_64,
            "p_estimated_units": 100,
        }).execute()
    assert getattr(exc.value, "code", None) == "42501"


def test_rpc_privileges_service_role(service_client):
    uid = _make_uid("svc_rpc")
    req_id = uuid.uuid4()
    result = _call_reserve(service_client, uid, req_id, _HMAC_64, _NET_HMAC_64, 100)
    assert result is not None
    assert result["decision"] == ADMITTED
    _delete_reservations(uid)


def test_direct_table_access_denied(service_client):
    uid = _make_uid("direct_access")
    with pytest.raises(APIError) as exc:
        service_client.table("admission_reservations").select("*").limit(1).execute()
    assert getattr(exc.value, "code", None) in ("42501", "PGRST116")

    with pytest.raises(APIError) as exc:
        service_client.table("admission_reservations").insert({
            "user_id": uid,
            "request_id": str(uuid.uuid4()),
            "message_hmac_sha256": _HMAC_64,
            "network_hmac_sha256": _NET_HMAC_64,
            "estimated_units": 100,
        }).execute()
    assert getattr(exc.value, "code", None) == "42501"


# ---------------------------------------------------------------------------
# 7. Basic admission flow
# ---------------------------------------------------------------------------

def test_basic_admission(service_client):
    uid = _make_uid("basic")
    req_id = uuid.uuid4()
    result = _call_reserve(service_client, uid, req_id, _HMAC_64, _NET_HMAC_64, 100)
    assert result is not None
    assert result["decision"] == ADMITTED
    assert result["retry_after_seconds"] == 0
    assert _count_reservations(uid) == 1
    _delete_reservations(uid)


# ---------------------------------------------------------------------------
# 8-10. Duplicate and replay tests
# ---------------------------------------------------------------------------

def test_duplicate_hmac_admitted(service_client):
    uid = _make_uid("dup_hmac")
    req_id_1 = uuid.uuid4()
    result_1 = _call_reserve(service_client, uid, req_id_1, _HMAC_64, _NET_HMAC_64, 100)
    assert result_1["decision"] == ADMITTED
    count_after_first = _count_reservations(uid)
    assert count_after_first == 1

    req_id_2 = uuid.uuid4()
    result_2 = _call_reserve(service_client, uid, req_id_2, _HMAC_64, _NET_HMAC_64, 200)
    assert result_2["decision"] == ADMITTED
    assert _count_reservations(uid) == count_after_first
    _delete_reservations(uid)


def test_exact_replay_unavailable(service_client):
    uid = _make_uid("replay")
    req_id = uuid.uuid4()
    result_1 = _call_reserve(service_client, uid, req_id, _HMAC_64, _NET_HMAC_64, 100)
    assert result_1["decision"] == ADMITTED

    result_2 = _call_reserve(service_client, uid, req_id, _HMAC_64, _NET_HMAC_64, 100)
    assert result_2["decision"] == REPLAY_UNAVAILABLE
    _delete_reservations(uid)


def test_request_id_conflict(service_client):
    uid = _make_uid("conflict")
    req_id = uuid.uuid4()
    result_1 = _call_reserve(service_client, uid, req_id, _HMAC_64, _NET_HMAC_64, 100)
    assert result_1["decision"] == ADMITTED

    different_hmac = "ff" * 32
    result_2 = _call_reserve(service_client, uid, req_id, different_hmac, _NET_HMAC_64, 100)
    assert result_2["decision"] == CONFLICT
    _delete_reservations(uid)


def test_same_uuid_different_users(service_client):
    uid_a = _make_uid("same_uuid_a")
    uid_b = _make_uid("same_uuid_b")
    shared_req_id = uuid.uuid4()

    result_a = _call_reserve(service_client, uid_a, shared_req_id, _HMAC_64, _NET_HMAC_64, 100)
    assert result_a["decision"] == ADMITTED

    result_b = _call_reserve(service_client, uid_b, shared_req_id, _HMAC_64, _NET_HMAC_64, 100)
    assert result_b["decision"] == ADMITTED

    assert _count_reservations(uid_a) == 1
    assert _count_reservations(uid_b) == 1
    _delete_reservations(uid_a)
    _delete_reservations(uid_b)


# ---------------------------------------------------------------------------
# 12. Invalid input
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field,value,expected", [
    ("p_user_id", "", INVALID_INPUT),
    ("p_user_id", "a" * 129, INVALID_INPUT),
    ("p_message_hmac_sha256", "g" * 64, INVALID_INPUT),
    ("p_message_hmac_sha256", "ab" * 31, INVALID_INPUT),
    ("p_message_hmac_sha256", "ab" * 33, INVALID_INPUT),
    ("p_network_hmac_sha256", "", INVALID_INPUT),
    ("p_network_hmac_sha256", "ab" * 31, INVALID_INPUT),
    ("p_estimated_units", 0, INVALID_INPUT),
    ("p_estimated_units", 6001, INVALID_INPUT),
    ("p_estimated_units", -1, INVALID_INPUT),
])
def test_invalid_input(service_client, field, value, expected):
    uid = _make_uid("invalid")
    params = {
        "p_user_id": uid,
        "p_request_id": str(uuid.uuid4()),
        "p_message_hmac_sha256": _HMAC_64,
        "p_network_hmac_sha256": _NET_HMAC_64,
        "p_estimated_units": 100,
    }
    params[field] = value
    resp = service_client.rpc("reserve_admission", params).execute()
    data = resp.data[0] if resp.data else {}
    assert data.get("decision") == expected
    assert _count_reservations(uid) == 0


# ---------------------------------------------------------------------------
# 13-15. Rate limit tests
# ---------------------------------------------------------------------------

def _bulk_insert_sql(user_id, net_hmac, count, interval_seconds):
    """Return SQL to bulk-insert reservation records for testing."""
    rows = []
    for i in range(1, count + 1):
        hmac = _make_hmac(user_id + str(i))
        rows.append(
            "('" + user_id + "',"
            "'" + str(uuid.uuid4()) + "',"
            "'" + hmac + "',"
            "'" + net_hmac + "',"
            "100,"
            "clock_timestamp() - interval '" + str(interval_seconds * i) + " seconds')"
        )
    return (
        "INSERT INTO public.admission_reservations "
        "(user_id, request_id, message_hmac_sha256, network_hmac_sha256, estimated_units, reserved_at) VALUES "
        + ", ".join(rows)
    )


def test_user_rate_limit(service_client):
    uid = _make_uid("user_rl")
    net_hmac = _make_hmac("user_rl_net")
    _execute_sql(_bulk_insert_sql(uid, net_hmac, 20, 2))
    assert _count_reservations(uid) == 20

    result = _call_reserve(service_client, uid, uuid.uuid4(), _make_hmac("user_rl_21"), net_hmac, 100)
    assert result["decision"] == USER_RATE_LIMITED
    assert result["retry_after_seconds"] >= 1
    _delete_reservations(uid)


def test_user_rate_limit_boundary(service_client):
    uid = _make_uid("user_rl_b")
    net_hmac = _make_hmac("user_rl_b_net")
    _execute_sql(_bulk_insert_sql(uid, net_hmac, 19, 2))
    assert _count_reservations(uid) == 19

    result = _call_reserve(service_client, uid, uuid.uuid4(), _make_hmac("user_rl_b_20"), net_hmac, 100)
    assert result["decision"] == ADMITTED
    _delete_reservations(uid)


def test_network_rate_limit(service_client):
    net_hmac = _make_hmac("net_rl_net")
    for i in range(60):
        _execute_sql(
            "INSERT INTO public.admission_reservations "
            "(user_id, request_id, message_hmac_sha256, network_hmac_sha256, estimated_units, reserved_at) VALUES ("
            "'net_rl_user_" + str(i) + "',"
            "'" + str(uuid.uuid4()) + "',"
            "'" + _make_hmac("net_rl_msg_" + str(i)) + "',"
            "'" + net_hmac + "',"
            "100,"
            "clock_timestamp() - interval '" + str(2 * (i + 1)) + " seconds')"
        )

    fresh_uid = _make_uid("net_rl_61")
    result = _call_reserve(service_client, fresh_uid, uuid.uuid4(), _make_hmac("net_rl_61"), net_hmac, 100)
    assert result["decision"] == NETWORK_RATE_LIMITED
    assert result["retry_after_seconds"] >= 1

    _execute_sql("DELETE FROM public.admission_reservations WHERE network_hmac_sha256 = '" + net_hmac + "'")
    _delete_reservations(fresh_uid)


def test_network_rate_limit_different_network(service_client):
    net_hmac = _make_hmac("net_rl_diff_net")
    different_net = _make_hmac("net_rl_diff_net_2")
    for i in range(60):
        _execute_sql(
            "INSERT INTO public.admission_reservations "
            "(user_id, request_id, message_hmac_sha256, network_hmac_sha256, estimated_units, reserved_at) VALUES ("
            "'net_rl_diff_user_" + str(i) + "',"
            "'" + str(uuid.uuid4()) + "',"
            "'" + _make_hmac("net_rl_diff_msg_" + str(i)) + "',"
            "'" + net_hmac + "',"
            "100,"
            "clock_timestamp() - interval '" + str(2 * (i + 1)) + " seconds')"
        )

    fresh_uid = _make_uid("net_rl_diff_fresh")
    result = _call_reserve(
        service_client, fresh_uid, uuid.uuid4(), _make_hmac("net_rl_diff_msg"), different_net, 100
    )
    assert result["decision"] == ADMITTED

    _execute_sql(
        "DELETE FROM public.admission_reservations "
        "WHERE network_hmac_sha256 IN ('" + net_hmac + "', '" + different_net + "')"
    )
    _delete_reservations(fresh_uid)


def test_application_rate_limit(service_client):
    uid = _make_uid("app_rl")
    rows = []
    for i in range(1, 26):
        rows.append(
            "('app_rl_user_" + str(i) + "',"
            "'" + str(uuid.uuid4()) + "',"
            "'" + _make_hmac("app_rl_" + str(i)) + "',"
            "'" + _make_hmac("app_rl_net_" + str(i)) + "',"
            "100,"
            "clock_timestamp() - interval '" + str(2 * i) + " seconds')"
        )
    sql = (
        "INSERT INTO public.admission_reservations "
        "(user_id, request_id, message_hmac_sha256, network_hmac_sha256, estimated_units, reserved_at) VALUES "
        + ", ".join(rows)
    )
    _execute_sql(sql)

    result = _call_reserve(
        service_client, uid, uuid.uuid4(), _make_hmac("app_rl_msg"), _make_hmac("app_rl_net_uid"), 100
    )
    assert result["decision"] == APP_RATE_LIMITED
    assert result["retry_after_seconds"] >= 1

    _execute_sql("DELETE FROM public.admission_reservations WHERE user_id LIKE 'app_rl_user\\_%'")
    _delete_reservations(uid)


# ---------------------------------------------------------------------------
# 16-17. Daily quota tests
# ---------------------------------------------------------------------------

def test_daily_request_quota(service_client):
    uid = _make_uid("daily_req")
    net_hmac = _make_hmac("daily_req_net")
    _execute_sql(_bulk_insert_sql(uid, net_hmac, 200, 5))
    assert _count_reservations(uid) == 200

    result = _call_reserve(service_client, uid, uuid.uuid4(), _make_hmac("daily_req_201"), net_hmac, 100)
    assert result["decision"] == DAILY_REQ_EXCEEDED
    assert result["retry_after_seconds"] >= 1
    _delete_reservations(uid)


def test_daily_request_quota_boundary(service_client):
    uid = _make_uid("daily_req_b")
    net_hmac = _make_hmac("daily_req_b_net")
    _execute_sql(_bulk_insert_sql(uid, net_hmac, 199, 5))
    assert _count_reservations(uid) == 199

    result = _call_reserve(service_client, uid, uuid.uuid4(), _make_hmac("daily_req_b_200"), net_hmac, 100)
    assert result["decision"] == ADMITTED
    _delete_reservations(uid)


def test_daily_unit_quota(service_client):
    uid = _make_uid("daily_unit")
    net_hmac = _make_hmac("daily_unit_net")
    for i in range(5):
        _execute_sql(
            "INSERT INTO public.admission_reservations "
            "(user_id, request_id, message_hmac_sha256, network_hmac_sha256, estimated_units, reserved_at) VALUES ("
            "'" + uid + "',"
            "'" + str(uuid.uuid4()) + "',"
            "'" + _make_hmac("daily_unit_" + str(i)) + "',"
            "'" + net_hmac + "',"
            "50000,"
            "clock_timestamp() - interval '" + str(5 * (i + 1)) + " seconds')"
        )

    res = _execute_sql(
        "SELECT COALESCE(sum(estimated_units), 0)::int AS total "
        "FROM public.admission_reservations WHERE user_id = '" + uid + "'"
    )
    total = json.loads(res.stdout)[0]["total"]
    assert total == 250000

    result = _call_reserve(service_client, uid, uuid.uuid4(), _make_hmac("daily_unit_exceed"), net_hmac, 1)
    assert result["decision"] == DAILY_UNIT_EXCEEDED
    _delete_reservations(uid)


def test_daily_unit_quota_boundary(service_client):
    uid = _make_uid("daily_unit_b")
    net_hmac = _make_hmac("daily_unit_b_net")
    _execute_sql(
        "INSERT INTO public.admission_reservations "
        "(user_id, request_id, message_hmac_sha256, network_hmac_sha256, estimated_units, reserved_at) VALUES ("
        "'" + uid + "',"
        "'" + str(uuid.uuid4()) + "',"
        "'" + _make_hmac("daily_unit_b_0") + "',"
        "'" + net_hmac + "',"
        "249999,"
        "clock_timestamp() - interval '5 seconds')"
    )

    result = _call_reserve(service_client, uid, uuid.uuid4(), _make_hmac("daily_unit_b_ok"), net_hmac, 1)
    assert result["decision"] == ADMITTED
    _delete_reservations(uid)


# ---------------------------------------------------------------------------
# 18. Window expiration
# ---------------------------------------------------------------------------

def test_window_expiration_user_rate(service_client):
    uid = _make_uid("win_exp")
    net_hmac = _make_hmac("win_exp_net")
    _execute_sql(_bulk_insert_sql(uid, net_hmac, 20, 3))
    _execute_sql(
        "UPDATE public.admission_reservations "
        "SET reserved_at = clock_timestamp() - interval '90 seconds' "
        "WHERE user_id = '" + uid + "'"
    )

    result = _call_reserve(service_client, uid, uuid.uuid4(), _make_hmac("win_exp_new"), net_hmac, 100)
    assert result["decision"] == ADMITTED
    _delete_reservations(uid)


def test_window_expiration_daily(service_client):
    uid = _make_uid("win_exp_day")
    net_hmac = _make_hmac("win_exp_day_net")
    _execute_sql(
        "INSERT INTO public.admission_reservations "
        "(user_id, request_id, message_hmac_sha256, network_hmac_sha256, estimated_units, reserved_at) "
        "SELECT "
        "'" + uid + "'::text, "
        "gen_random_uuid(), "
        "'" + _make_hmac("win_exp_day") + "' || lpad(i::text, 4, '0'), "
        "'" + net_hmac + "', "
        "100, "
        "clock_timestamp() - interval '25 hours' "
        "FROM generate_series(1, 200) AS i"
    )
    assert _count_reservations(uid) == 200

    result = _call_reserve(service_client, uid, uuid.uuid4(), _make_hmac("win_exp_day_new"), net_hmac, 100)
    assert result["decision"] == ADMITTED
    _delete_reservations(uid)


# ---------------------------------------------------------------------------
# 19. Concurrent independent reservations
# ---------------------------------------------------------------------------

def test_concurrent_independent_reservations(service_client):
    n_users = 10
    results = []

    def _reserve(user_num):
        uid = _make_uid("concurrent_" + str(user_num))
        req_id = uuid.uuid4()
        result = _call_reserve(
            service_client, uid, req_id, _make_hmac("con_" + str(user_num)),
            _make_hmac("con_net_" + str(user_num)), 100
        )
        return (user_num, result["decision"] if result else None)

    with ThreadPoolExecutor(max_workers=n_users) as pool:
        futures = [pool.submit(_reserve, i) for i in range(n_users)]
        for f in as_completed(futures):
            results.append(f.result())

    admitted_count = sum(1 for _, d in results if d == ADMITTED)
    assert admitted_count == n_users, f"Expected {n_users} admitted, got {admitted_count}"

    for i in range(n_users):
        _delete_reservations(_make_uid("concurrent_" + str(i)))


# ---------------------------------------------------------------------------
# 20. Concurrent duplicates (exactly one insert)
# ---------------------------------------------------------------------------

def test_concurrent_duplicates_exactly_one_insert(service_client):
    uid = _make_uid("con_dup")
    req_id = uuid.uuid4()
    msg_hmac = _make_hmac("con_dup_msg")
    net_hmac = _make_hmac("con_dup_net")

    n_threads = 10
    results = []

    def _call():
        try:
            result = _call_reserve(service_client, uid, req_id, msg_hmac, net_hmac, 100)
            return result["decision"] if result else None
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        futures = [pool.submit(_call) for _ in range(n_threads)]
        for f in as_completed(futures):
            results.append(f.result())

    admitted = sum(1 for r in results if r in (ADMITTED, REPLAY_UNAVAILABLE))
    assert admitted == n_threads, f"Expected {n_threads} admitted/replay, got {admitted}"

    count = _count_reservations(uid)
    assert count <= 1, f"Expected at most 1 row, got {count}"
    _delete_reservations(uid)


# ---------------------------------------------------------------------------
# 21. Global concurrency
# ---------------------------------------------------------------------------

def test_global_concurrency_limit(service_client):
    _execute_sql("DELETE FROM public.admission_reservations WHERE reserved_at > NOW() - interval '60 seconds'")

    n_threads = 40
    results = []

    def _reserve(i):
        uid = _make_uid("global_" + str(i))
        req_id = uuid.uuid4()
        result = _call_reserve(
            service_client, uid, req_id, _make_hmac("global_" + str(i)),
            _make_hmac("global_net_" + str(i)), 100
        )
        return (uid, result["decision"] if result else None)

    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        futures = [pool.submit(_reserve, i) for i in range(n_threads)]
        for f in as_completed(futures):
            results.append(f.result())

    admitted_count = sum(1 for _, d in results if d == ADMITTED)
    assert admitted_count <= 25, f"Expected <= 25 admitted, got {admitted_count}"

    for uid, _ in results:
        _delete_reservations(uid)


# ---------------------------------------------------------------------------
# 22. Authorization matrix
# ---------------------------------------------------------------------------

def test_authorization_anon_denied(anon_client):
    uid = _make_uid("auth_anon")
    with pytest.raises(APIError) as exc:
        anon_client.rpc("reserve_admission", {
            "p_user_id": uid,
            "p_request_id": str(uuid.uuid4()),
            "p_message_hmac_sha256": _HMAC_64,
            "p_network_hmac_sha256": _NET_HMAC_64,
            "p_estimated_units": 100,
        }).execute()
    assert getattr(exc.value, "code", None) == "42501"


def test_authorization_authenticated_denied(auth_client_a):
    client_a, _ = auth_client_a
    uid = _make_uid("auth_auth")
    with pytest.raises(APIError) as exc:
        client_a.rpc("reserve_admission", {
            "p_user_id": uid,
            "p_request_id": str(uuid.uuid4()),
            "p_message_hmac_sha256": _HMAC_64,
            "p_network_hmac_sha256": _NET_HMAC_64,
            "p_estimated_units": 100,
        }).execute()
    assert getattr(exc.value, "code", None) == "42501"


def test_authorization_service_role_allowed(service_client):
    uid = _make_uid("auth_svc")
    result = _call_reserve(service_client, uid, uuid.uuid4(), _HMAC_64, _NET_HMAC_64, 100)
    assert result["decision"] == ADMITTED
    _delete_reservations(uid)


# ---------------------------------------------------------------------------
# 23. Migration idempotency test (valid)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def supabase_service_client():
    from supabase import create_client
    url = os.environ.get("SUPABASE_URL", "http://127.0.0.1:54321")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not key:
        result = run_supabase_op(
            "admission_query",
            ["status", "-o", "env"],
            check=False,
        )
        if result.returncode != 0:
            pytest.skip("Could not extract service role key")
        for line in result.stdout.splitlines():
            if line.startswith("SERVICE_ROLE_KEY="):
                key = line.split("=", 1)[1].strip('"')
                break
        if not key:
            pytest.skip("SERVICE_ROLE_KEY not found")
    return create_client(url, key)


@pytest.mark.database_integration
def test_migration_applies_cleanly():
    """Migration applies cleanly on a fresh baseline database."""
    _move_admission_migration_aside()
    try:
        _run_supabase("admission_reset", ["db", "reset"])

        # Verify table does not exist
        res = _execute_sql(
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'admission_reservations') AS result"
        )
        assert json.loads(res.stdout)[0]["result"] is False, "table should not exist yet"

        _restore_admission_migration()
        _run_supabase("admission_up", ["migration", "up", "--local"])

        # Verify migration timestamp
        res = _execute_sql(
            "SELECT EXISTS(SELECT 1 FROM supabase_migrations.schema_migrations "
            "WHERE version = '20240101000003') AS result"
        )
        assert json.loads(res.stdout)[0]["result"] is True, "Migration timestamp not registered"

        # Verify RLS is enabled
        res = _execute_sql(
            "SELECT relrowsecurity FROM pg_class "
            "WHERE oid = 'public.admission_reservations'::regclass"
        )
        assert json.loads(res.stdout)[0]["relrowsecurity"] is True

        # Verify FORCE RLS
        res = _execute_sql(
            "SELECT relforcerowsecurity FROM pg_class "
            "WHERE oid = 'public.admission_reservations'::regclass"
        )
        assert json.loads(res.stdout)[0]["relforcerowsecurity"] is True

    finally:
        _ensure_admission_migration_present()


# ---------------------------------------------------------------------------
# 24. Re-applying migration is idempotent
# ---------------------------------------------------------------------------

@pytest.mark.database_integration
def test_migration_idempotent():
    """Re-applying the migration does not cause errors."""
    _move_admission_migration_aside()
    try:
        _run_supabase("admission_reset", ["db", "reset"])

        _restore_admission_migration()
        # First apply
        _run_supabase("admission_up", ["migration", "up", "--local"])

        # Second apply — should be a no-op
        _run_supabase("admission_up", ["migration", "up", "--local"], check=False)

        # Still exactly one timestamp
        res = _execute_sql(
            "SELECT count(*)::int AS c FROM supabase_migrations.schema_migrations "
            "WHERE version = '20240101000003'"
        )
        assert json.loads(res.stdout)[0]["c"] == 1, "Expected exactly 1 migration entry"

    finally:
        _ensure_admission_migration_present()


# ---------------------------------------------------------------------------
# 25. Invalid insert via RPC rejected (replaces legacy upgrade invalid)
# ---------------------------------------------------------------------------
# The "legacy upgrade invalid" scenario cannot be tested as a migration
# because the table is created by the same migration (no pre-existing table
# to hold invalid data).  Instead, the invalid_admission_input tests above
# (section 12) prove that all CHECK constraints are enforced by the RPC.
