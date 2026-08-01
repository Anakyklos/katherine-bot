"""Real Supabase integration tests for the PostgreSQL admission ledger.

This file is executed only by the database CI job against a freshly reset local
Supabase instance. It must never be collected by the ordinary backend job.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest
from postgrest.exceptions import APIError
from supabase import Client, create_client

ADMITTED = "admitted"
REPLAY_UNAVAILABLE = "request_replay_unavailable"
CONFLICT = "request_id_conflict"
APP_RATE_LIMITED = "application_rate_limited"

MESSAGE_HMAC_A = "a" * 64
MESSAGE_HMAC_B = "b" * 64
NETWORK_HMAC = "c" * 64


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    assert value, f"{name} is required for admission ledger integration tests"
    return value


@pytest.fixture(scope="module")
def supabase_url() -> str:
    return _required_env("SUPABASE_URL")


@pytest.fixture(scope="module")
def anon_key() -> str:
    return _required_env("SUPABASE_ANON_KEY")


@pytest.fixture(scope="module")
def service_role_key() -> str:
    return _required_env("SUPABASE_SERVICE_ROLE_KEY")


def _close_http_transports(client: Client) -> None:
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


def _close_client(client: Client) -> None:
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
def service_client(supabase_url: str, service_role_key: str) -> Client:
    client = create_client(supabase_url, service_role_key)
    yield client
    _close_client(client)


@pytest.fixture(scope="module")
def anon_client(supabase_url: str, anon_key: str) -> Client:
    client = create_client(supabase_url, anon_key)
    yield client
    _close_client(client)


def _run_sql(sql: str) -> list[dict]:
    """Execute trusted test SQL through the pinned local Supabase CLI.

    SELECT statements are emitted as JSON. PostgreSQL command tags such as
    ``TRUNCATE TABLE`` are successful commands without a row result and map to
    an empty list instead of being treated as JSON.
    """
    result = subprocess.run(
        [
            "supabase",
            "db",
            "query",
            "--agent=no",
            "--output",
            "json",
            sql,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, "sanitized admission test SQL operation failed"
    output = result.stdout.strip()
    if not output or output[0] not in "[{":
        return []
    parsed = json.loads(output)
    assert isinstance(parsed, list)
    return parsed


def _truncate_ledger() -> None:
    _run_sql("TRUNCATE TABLE public.admission_reservations")


def _count_rows() -> int:
    rows = _run_sql(
        "SELECT count(*)::integer AS count FROM public.admission_reservations"
    )
    return rows[0]["count"]


def _reserve(
    client: Client,
    *,
    user_id: str,
    request_id: uuid.UUID,
    message_hmac: str = MESSAGE_HMAC_A,
    network_hmac: str = NETWORK_HMAC,
    estimated_units: int = 100,
) -> dict:
    response = client.rpc(
        "reserve_admission",
        {
            "p_user_id": user_id,
            "p_request_id": str(request_id),
            "p_message_hmac_sha256": message_hmac,
            "p_network_hmac_sha256": network_hmac,
            "p_estimated_units": estimated_units,
        },
    ).execute()
    assert isinstance(response.data, list)
    assert len(response.data) == 1
    result = response.data[0]
    assert set(result) == {"decision", "retry_after_seconds"}
    return result


def _assert_valid_session(response, client: Client, label: str) -> str:
    assert response is not None, f"{label}: missing auth response"
    assert response.user is not None, f"{label}: missing user"
    assert response.session is not None, f"{label}: missing session"
    assert response.session.access_token, f"{label}: missing access token"
    fetched = client.auth.get_user()
    assert fetched is not None and fetched.user is not None
    assert fetched.user.id == response.user.id
    return response.user.id


@pytest.fixture(scope="module")
def authenticated_client(
    supabase_url: str,
    anon_key: str,
    service_client: Client,
) -> Client:
    email = "admission-ledger-auth@test.local"
    password = "password123"

    for user in service_client.auth.admin.list_users():
        if user.email == email:
            service_client.auth.admin.delete_user(user.id)

    client = create_client(supabase_url, anon_key)
    client.auth.sign_up({"email": email, "password": password})
    response = client.auth.sign_in_with_password(
        {"email": email, "password": password}
    )
    _assert_valid_session(response, client, "authenticated_client")
    yield client

    # Close the lazily-created transports BEFORE sign_out(): sign_out() fires
    # an auth state change event that resets client._postgrest (and the other
    # lazy transports) to None without closing their httpx sessions, which
    # would leak sockets until GC and fail under filterwarnings=error.
    _close_http_transports(client)
    client.auth.sign_out()
    for user in service_client.auth.admin.list_users():
        if user.email == email:
            service_client.auth.admin.delete_user(user.id)
    _close_client(client)


@pytest.fixture(autouse=True)
def clean_ledger():
    _truncate_ledger()
    yield
    _truncate_ledger()


def _rpc_params() -> dict:
    return {
        "p_user_id": "authorization-user",
        "p_request_id": str(uuid.uuid4()),
        "p_message_hmac_sha256": MESSAGE_HMAC_A,
        "p_network_hmac_sha256": NETWORK_HMAC,
        "p_estimated_units": 100,
    }


def test_anon_cannot_execute_rpc(anon_client: Client):
    with pytest.raises(APIError) as exc_info:
        anon_client.rpc("reserve_admission", _rpc_params()).execute()
    assert getattr(exc_info.value, "code", None) == "42501"


def test_authenticated_user_cannot_execute_rpc(authenticated_client: Client):
    with pytest.raises(APIError) as exc_info:
        authenticated_client.rpc("reserve_admission", _rpc_params()).execute()
    assert getattr(exc_info.value, "code", None) == "42501"


def test_service_role_can_execute_rpc(service_client: Client):
    result = _reserve(
        service_client,
        user_id="service-role-rpc",
        request_id=uuid.uuid4(),
    )
    assert result == {"decision": ADMITTED, "retry_after_seconds": 0}


@pytest.mark.parametrize("operation", ["select", "insert", "update", "delete"])
def test_service_role_has_no_direct_table_access(
    service_client: Client,
    operation: str,
):
    table = service_client.table("admission_reservations")
    if operation == "select":
        request = table.select("*").limit(1)
    elif operation == "insert":
        request = table.insert(
            {
                "user_id": "direct-access",
                "request_id": str(uuid.uuid4()),
                "message_hmac_sha256": MESSAGE_HMAC_A,
                "network_hmac_sha256": NETWORK_HMAC,
                "estimated_units": 100,
            }
        )
    elif operation == "update":
        request = table.update({"estimated_units": 101}).eq(
            "user_id", "direct-access"
        )
    else:
        request = table.delete().eq("user_id", "direct-access")

    with pytest.raises(APIError) as exc_info:
        request.execute()
    assert getattr(exc_info.value, "code", None) == "42501"


def _new_service_client(url: str, key: str) -> Client:
    return create_client(url, key)


@dataclass(frozen=True)
class ConcurrentCall:
    user_id: str
    request_id: uuid.UUID
    message_hmac: str
    network_hmac: str


def _concurrent_reserve(
    *,
    url: str,
    key: str,
    barrier: threading.Barrier,
    call: ConcurrentCall,
) -> dict:
    client = _new_service_client(url, key)
    try:
        barrier.wait(timeout=10)
        return _reserve(
            client,
            user_id=call.user_id,
            request_id=call.request_id,
            message_hmac=call.message_hmac,
            network_hmac=call.network_hmac,
        )
    finally:
        _close_client(client)


def test_concurrent_exact_replays_insert_once(
    supabase_url: str,
    service_role_key: str,
):
    workers = 10
    barrier = threading.Barrier(workers)
    request_id = uuid.uuid4()
    call = ConcurrentCall(
        user_id="concurrent-replay",
        request_id=request_id,
        message_hmac=MESSAGE_HMAC_A,
        network_hmac=NETWORK_HMAC,
    )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                _concurrent_reserve,
                url=supabase_url,
                key=service_role_key,
                barrier=barrier,
                call=call,
            )
            for _ in range(workers)
        ]
        results = [future.result(timeout=20) for future in futures]

    decisions = [result["decision"] for result in results]
    assert decisions.count(ADMITTED) == 1
    assert decisions.count(REPLAY_UNAVAILABLE) == workers - 1
    assert _count_rows() == 1


def test_concurrent_conflict_has_one_winner(
    supabase_url: str,
    service_role_key: str,
):
    workers = 2
    barrier = threading.Barrier(workers)
    request_id = uuid.uuid4()
    calls = [
        ConcurrentCall(
            user_id="concurrent-conflict",
            request_id=request_id,
            message_hmac=MESSAGE_HMAC_A,
            network_hmac=NETWORK_HMAC,
        ),
        ConcurrentCall(
            user_id="concurrent-conflict",
            request_id=request_id,
            message_hmac=MESSAGE_HMAC_B,
            network_hmac=NETWORK_HMAC,
        ),
    ]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                _concurrent_reserve,
                url=supabase_url,
                key=service_role_key,
                barrier=barrier,
                call=call,
            )
            for call in calls
        ]
        results = [future.result(timeout=20) for future in futures]

    decisions = sorted(result["decision"] for result in results)
    assert decisions == sorted([ADMITTED, CONFLICT])
    assert _count_rows() == 1


def test_same_uuid_isolated_between_users(
    supabase_url: str,
    service_role_key: str,
):
    workers = 2
    barrier = threading.Barrier(workers)
    request_id = uuid.uuid4()
    calls = [
        ConcurrentCall("isolated-user-a", request_id, MESSAGE_HMAC_A, "d" * 64),
        ConcurrentCall("isolated-user-b", request_id, MESSAGE_HMAC_B, "e" * 64),
    ]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                _concurrent_reserve,
                url=supabase_url,
                key=service_role_key,
                barrier=barrier,
                call=call,
            )
            for call in calls
        ]
        results = [future.result(timeout=20) for future in futures]

    assert [result["decision"] for result in results].count(ADMITTED) == 2
    assert _count_rows() == 2


def test_global_concurrency_admits_exactly_25(
    supabase_url: str,
    service_role_key: str,
):
    workers = 40
    barrier = threading.Barrier(workers)
    calls = [
        ConcurrentCall(
            user_id=f"global-user-{index}",
            request_id=uuid.uuid4(),
            message_hmac=f"{index + 1:064x}",
            network_hmac=f"{index + 1000:064x}",
        )
        for index in range(workers)
    ]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                _concurrent_reserve,
                url=supabase_url,
                key=service_role_key,
                barrier=barrier,
                call=call,
            )
            for call in calls
        ]
        results = [future.result(timeout=30) for future in futures]

    decisions = [result["decision"] for result in results]
    assert decisions.count(ADMITTED) == 25
    assert decisions.count(APP_RATE_LIMITED) == workers - 25
    assert all(
        result["retry_after_seconds"] == 60
        for result in results
        if result["decision"] == APP_RATE_LIMITED
    )
    assert _count_rows() == 25
