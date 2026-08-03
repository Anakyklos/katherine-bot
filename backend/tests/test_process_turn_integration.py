"""Real Supabase integration tests for the ProcessTurn foundation (#272).

Executed ONLY by the database CI job against a freshly reset local Supabase
instance (no mocks: real PostgreSQL transactions, locks, FKs, leases and
rollbacks). Never collected by the ordinary backend unit job.

Covers:

  1. replay_committed_turn RPC behavior against real rows
  2. replay RPC authorization matrix (anon / authenticated / service_role)
  3. replay does not alter any table; repeated replays are equivalent
  4. multi-worker consistency using INDEPENDENT subprocess workers, each with
     its own Supabase client (never two coroutines over one client)
  5. outbox atomicity (one event, replay adds none, rollback removes it)
  6. idempotency: same request id replays; divergent message conflicts
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from postgrest.exceptions import APIError
from supabase import Client, create_client

from backend.emotional_domain import EmotionalStateV1
from backend.relationship import RelationshipStateV1

_SUPABASE_CLI = ["supabase"] if shutil.which("supabase") else ["npx", "supabase"]


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    assert value, f"{name} is required for process turn integration tests"
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


@pytest.fixture(scope="module")
def authenticated_client(
    supabase_url: str,
    anon_key: str,
    service_client: Client,
) -> Client:
    email = "process-turn-replay-auth@test.local"
    password = "password123"
    client = create_client(supabase_url, anon_key)
    for user in service_client.auth.admin.list_users():
        if user.email == email:
            service_client.auth.admin.delete_user(user.id)
    client.auth.sign_up({"email": email, "password": password})
    client.auth.sign_in_with_password({"email": email, "password": password})
    yield client
    _close_http_transports(client)
    client.auth.sign_out()
    for user in service_client.auth.admin.list_users():
        if user.email == email:
            service_client.auth.admin.delete_user(user.id)
    _close_client(client)


def _run_sql(sql: str) -> list[dict]:
    child_env = dict(os.environ)
    child_env["SUPABASE_TELEMETRY_DISABLED"] = "1"
    child_env["SUPABASE_ANALYTICS_ENABLED"] = "false"
    result = subprocess.run(
        [
            *_SUPABASE_CLI,
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
        env=child_env,
    )
    assert result.returncode == 0, "sanitized process turn test SQL operation failed"
    output = result.stdout.strip()
    if not output or output[0] not in "[{":
        return []
    parsed = json.loads(output)
    assert isinstance(parsed, list)
    return parsed


def _count(table: str, user_id: str) -> int:
    rows = _run_sql(
        f"SELECT count(*)::integer AS count FROM public.{table} "
        f"WHERE user_id = '{user_id}'"
    )
    return rows[0]["count"]


def _uid(label: str) -> str:
    return f"pt_{label}_{uuid.uuid4().hex[:12]}"


def _emotional_state() -> dict:
    return EmotionalStateV1.neutral(timestamp=1700000000.0).to_dict()


def _relationship_state() -> dict:
    return RelationshipStateV1.neutral(timestamp=1700000000.0).to_dict()


def _replay_payload(response: str = "Hi there!") -> dict:
    return {"response": response, "message_id": str(uuid.uuid4()), "duration_ms": 42}


def _commit_params(
    user_id: str,
    request_id: str,
    *,
    payload_hash: str,
    expected_revision: int = 0,
    response: str = "Hi there!",
    outbox_events: list[dict] | None = None,
) -> dict:
    return {
        "p_authenticated_user_id": user_id,
        "p_request_id": request_id,
        "p_expected_revision": expected_revision,
        "p_user_message": "Hello",
        "p_assistant_message": response,
        "p_payload_hash_sha256": payload_hash,
        "p_emotional_state": _emotional_state(),
        "p_relationship_state": _relationship_state(),
        "p_public_response": response,
        "p_replay_payload": _replay_payload(response),
        "p_outbox_events": outbox_events if outbox_events is not None else [],
        "p_lease_owner": "worker-1",
    }


def _call_commit(client: Client, params: dict) -> dict:
    response = client.rpc("commit_turn", params).execute()
    data = response.data
    if isinstance(data, list):
        assert len(data) == 1
        data = data[0]
    assert isinstance(data, dict)
    return data


def _call_replay(client: Client, user_id: str, request_id: str) -> dict:
    response = client.rpc(
        "replay_committed_turn",
        {"p_authenticated_user_id": user_id, "p_request_id": request_id},
    ).execute()
    data = response.data
    if isinstance(data, list):
        assert len(data) == 1
        data = data[0]
    assert isinstance(data, dict)
    return data


def _normalized(result: dict) -> dict:
    assert "error" not in result and "status" not in result
    return {
        "user_id": result["user_id"],
        "request_id": result["request_id"],
        "committed_revision": result["committed_revision"],
        "user_message_id": result["user_message_id"],
        "assistant_message_id": result["assistant_message_id"],
        "replay_payload": result["replay_payload"],
        "outbox_events": result["outbox_events"],
        "created_at": result["created_at"],
        "completed_at": result["completed_at"],
    }


def _cleanup_user(client: Client, user_id: str) -> None:
    try:
        client.table("chat_logs").delete().eq("user_id", user_id).execute()
    except Exception:
        pass
    try:
        client.table("profiles").delete().eq("user_id", user_id).execute()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 1. replay_committed_turn RPC behavior
# ---------------------------------------------------------------------------


def test_completed_turn_replays_canonical_contract(service_client: Client):
    user_id = _uid("replay_ok")
    request_id = str(uuid.uuid4())
    try:
        first = _call_commit(
            service_client, _commit_params(user_id, request_id, payload_hash="a" * 64)
        )
        assert "error" not in first

        replay = _call_replay(service_client, user_id, request_id)
        # Exactly the canonical CommittedTurn contract (no status marker).
        assert "status" not in replay
        assert _normalized(replay) == _normalized(first)
        assert replay["replay_payload"]["response"] == "Hi there!"
    finally:
        _cleanup_user(service_client, user_id)


def test_user_a_cannot_recover_user_b_turn(service_client: Client):
    user_a = _uid("user_a")
    user_b = _uid("user_b")
    request_id = str(uuid.uuid4())
    try:
        result = _call_commit(
            service_client, _commit_params(user_a, request_id, payload_hash="a" * 64)
        )
        assert "error" not in result
        replay = _call_replay(service_client, user_b, request_id)
        assert replay == {"status": "request_replay_unavailable"}
    finally:
        _cleanup_user(service_client, user_a)
        _cleanup_user(service_client, user_b)


def test_missing_request_returns_structured_result(service_client: Client):
    user_id = _uid("missing")
    replay = _call_replay(service_client, user_id, str(uuid.uuid4()))
    assert replay == {"status": "request_replay_unavailable"}


def test_pending_request_is_not_treated_as_completed(service_client: Client):
    user_id = _uid("pending")
    request_id = str(uuid.uuid4())
    try:
        _run_sql(
            "INSERT INTO public.profiles (user_id) VALUES "
            f"('{user_id}')"
        )
        _run_sql(
            "INSERT INTO public.turn_requests "
            "(user_id, request_id, payload_hash_sha256, status, lease_owner, lease_expires_at) "
            f"VALUES ('{user_id}', '{request_id}', '{'b' * 64}', 'pending', "
            "'worker-x', timezone('utc'::text, now()) + INTERVAL '1 hour')"
        )
        replay = _call_replay(service_client, user_id, request_id)
        assert replay == {"status": "request_in_progress"}
    finally:
        _cleanup_user(service_client, user_id)


def test_expired_request_is_not_treated_as_completed(service_client: Client):
    user_id = _uid("expired")
    request_id = str(uuid.uuid4())
    try:
        _run_sql(
            "INSERT INTO public.profiles (user_id) VALUES "
            f"('{user_id}')"
        )
        _run_sql(
            "INSERT INTO public.turn_requests "
            "(user_id, request_id, payload_hash_sha256, status, error_code) "
            f"VALUES ('{user_id}', '{request_id}', '{'c' * 64}', 'expired', 'timeout')"
        )
        replay = _call_replay(service_client, user_id, request_id)
        assert replay == {"status": "request_replay_unavailable"}
    finally:
        _cleanup_user(service_client, user_id)


def test_invalid_identity_fails_sanitized(service_client: Client):
    result = _call_replay(service_client, "", str(uuid.uuid4()))
    assert result["error"]["code"] == "validation_failed"
    text = json.dumps(result)
    # never echoes SQLSTATE, constraint names or raw input
    assert "42501" not in text
    assert "P0001" not in text


def test_replay_does_not_alter_tables_and_repeats_are_equivalent(
    service_client: Client,
):
    user_id = _uid("no_write")
    request_id = str(uuid.uuid4())
    try:
        _call_commit(service_client, _commit_params(user_id, request_id, payload_hash="a" * 64))
        before = (
            _count("chat_logs", user_id),
            _count("turn_requests", user_id),
            _count("outbox_events", user_id),
            _run_sql(f"SELECT revision FROM public.profiles WHERE user_id = '{user_id}'")[0]["revision"],
        )
        replay_a = _call_replay(service_client, user_id, request_id)
        replay_b = _call_replay(service_client, user_id, request_id)
        assert replay_a == replay_b
        after = (
            _count("chat_logs", user_id),
            _count("turn_requests", user_id),
            _count("outbox_events", user_id),
            _run_sql(f"SELECT revision FROM public.profiles WHERE user_id = '{user_id}'")[0]["revision"],
        )
        assert after == before
    finally:
        _cleanup_user(service_client, user_id)


# ---------------------------------------------------------------------------
# 2. replay_committed_turn authorization matrix
# ---------------------------------------------------------------------------


def test_anon_cannot_execute_replay_rpc(anon_client: Client):
    with pytest.raises(APIError) as exc_info:
        anon_client.rpc(
            "replay_committed_turn",
            {"p_authenticated_user_id": "x", "p_request_id": str(uuid.uuid4())},
        ).execute()
    assert getattr(exc_info.value, "code", None) == "42501"


def test_authenticated_cannot_execute_replay_rpc(authenticated_client: Client):
    with pytest.raises(APIError) as exc_info:
        authenticated_client.rpc(
            "replay_committed_turn",
            {"p_authenticated_user_id": "x", "p_request_id": str(uuid.uuid4())},
        ).execute()
    assert getattr(exc_info.value, "code", None) == "42501"


def test_service_role_can_execute_replay_rpc(service_client: Client):
    result = _call_replay(service_client, _uid("sr"), str(uuid.uuid4()))
    assert result == {"status": "request_replay_unavailable"}


# ---------------------------------------------------------------------------
# 3. Idempotency through commit_turn (used by the normal ProcessTurn path)
# ---------------------------------------------------------------------------


def test_same_request_after_commit_replays_without_new_writes(service_client: Client):
    user_id = _uid("retry_same")
    request_id = str(uuid.uuid4())
    try:
        first = _call_commit(
            service_client, _commit_params(user_id, request_id, payload_hash="a" * 64)
        )
        assert "error" not in first
        replay = _call_commit(
            service_client, _commit_params(user_id, request_id, payload_hash="a" * 64)
        )
        assert _normalized(replay) == _normalized(first)
        assert _count("chat_logs", user_id) == 2
        assert _count("turn_requests", user_id) == 1
    finally:
        _cleanup_user(service_client, user_id)


def test_same_request_different_message_conflicts(service_client: Client):
    user_id = _uid("diff_msg")
    request_id = str(uuid.uuid4())
    try:
        _call_commit(service_client, _commit_params(user_id, request_id, payload_hash="a" * 64))
        conflict = _call_commit(
            service_client, _commit_params(user_id, request_id, payload_hash="b" * 64)
        )
        assert conflict["error"]["code"] == "request_payload_conflict"
        assert _count("chat_logs", user_id) == 2
    finally:
        _cleanup_user(service_client, user_id)


# ---------------------------------------------------------------------------
# 4. Multi-worker consistency (independent subprocesses, separate clients)
# ---------------------------------------------------------------------------

# Each worker: loads the profile revision, synchronizes on a file barrier,
# then runs the ProcessTurn commit loop (bounded retry on revision_mismatch).
# It reports its attempts and the revision it committed at.
_WORKER_SCRIPT = r"""
import json
import os
import sys
import time
import uuid

from supabase import create_client

url = os.environ["SUPABASE_URL"]
key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
user_id = os.environ["WORKER_USER"]
request_id = os.environ["WORKER_REQUEST"]
ready_file = os.environ["WORKER_READY"]
peer_file = os.environ["WORKER_PEER"]

client = create_client(url, key)


def load_revision():
    rows = (
        client.table("profiles")
        .select("revision")
        .eq("user_id", user_id)
        .execute()
        .data
    )
    return rows[0]["revision"] if rows else 0


def commit(expected_revision, attempt):
    emotional = {
        "schema_version": 1, "pleasure": 0.1, "arousal": 0.1, "dominance": 0.1,
        "libido": 0.1, "aggression": 0.1, "connection": 0.5, "energy": 0.5,
        "tension": 0.1, "coping_mode": "HEALTHY", "timestamp": 1700000000.0,
    }
    relationship = {
        "schema_version": 1, "trust": 0.5, "affection": 0.3, "tension": 0.1,
        "triggers": [], "timestamp": 1700000000.0,
    }
    replay = {
        "response": "Hi from worker " + request_id[:8],
        "message_id": str(uuid.uuid4()),
        "duration_ms": 7,
    }
    params = {
        "p_authenticated_user_id": user_id,
        "p_request_id": request_id,
        "p_expected_revision": expected_revision,
        "p_user_message": "Hello",
        "p_assistant_message": replay["response"],
        "p_payload_hash_sha256": ("a" + str(attempt)) * 32,
        "p_emotional_state": emotional,
        "p_relationship_state": relationship,
        "p_public_response": replay["response"],
        "p_replay_payload": replay,
        "p_outbox_events": [],
        "p_lease_owner": "worker-" + str(attempt),
    }
    response = client.rpc("commit_turn", params).execute()
    data = response.data
    if isinstance(data, list):
        data = data[0]
    return data


def fail(reason):
    print(json.dumps({"ok": False, "reason": reason}))
    sys.exit(1)


# Load revision BEFORE the barrier so both workers race the same revision.
revision = load_revision()
open(ready_file, "w").close()
deadline = time.monotonic() + 30
while not os.path.exists(peer_file):
    if time.monotonic() > deadline:
        fail("barrier_timeout")
    time.sleep(0.02)

attempts = 0
for attempt in (1, 2):
    if attempt == 2:
        revision = load_revision()
    data = commit(revision, attempt)
    attempts = attempt
    if "error" not in data:
        print(
            json.dumps(
                {
                    "ok": True,
                    "attempts": attempts,
                    "revision": data["committed_revision"],
                    "request_id": data["request_id"],
                }
            )
        )
        sys.exit(0)
    if data["error"]["code"] != "revision_mismatch":
        fail(data["error"]["code"])
fail("exhausted")
"""


def _run_worker_pair(url: str, key: str, user_a: str, user_b: str) -> list[dict]:
    """Run two independent subprocess workers synchronized by a file barrier."""
    ready_dir = f"/tmp/opencode/pt_workers_{uuid.uuid4().hex}"
    os.makedirs(ready_dir, exist_ok=True)

    def spawn(user: str, index: int) -> tuple[subprocess.Popen, str, str, str]:
        request_id = str(uuid.uuid4())
        ready = os.path.join(ready_dir, f"ready_{index}")
        peer = os.path.join(ready_dir, f"ready_{1 - index}")
        env = dict(os.environ)
        env.update(
            SUPABASE_URL=url,
            SUPABASE_SERVICE_ROLE_KEY=key,
            WORKER_USER=user,
            WORKER_REQUEST=request_id,
            WORKER_READY=ready,
            WORKER_PEER=peer,
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", _WORKER_SCRIPT],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return proc, ready, request_id, user

    proc_a, ready_a, request_a, user_a_final = spawn(user_a, 0)
    proc_b, ready_b, request_b, _ = spawn(user_b, 1)
    assert ready_a != ready_b
    try:
        out_a, err_a = proc_a.communicate(timeout=60)
        out_b, err_b = proc_b.communicate(timeout=60)
    finally:
        proc_a.kill()
        proc_b.kill()
        for f in (ready_a, ready_b):
            try:
                os.unlink(f)
            except OSError:
                pass
        try:
            os.rmdir(ready_dir)
        except OSError:
            pass
    assert proc_a.returncode == 0, f"worker A failed: {err_a}"
    assert proc_b.returncode == 0, f"worker B failed: {err_b}"
    return [
        json.loads(out_a),
        json.loads(out_b),
    ]


def test_multi_worker_same_user_cas_consistency(supabase_url, service_role_key):
    """Two independent workers race the same revision: one wins, the other
    receives revision_mismatch, reloads and commits on the next revision."""
    user_id = _uid("mw_same")
    client = create_client(supabase_url, service_role_key)
    try:
        results = _run_worker_pair(supabase_url, service_role_key, user_id, user_id)
        assert [r["ok"] for r in results] == [True, True]
        attempts = sorted(r["attempts"] for r in results)
        # exactly one worker retried once; the other committed on the first try
        assert attempts == [1, 2]
        # the revision increased exactly twice (one per committed request)
        rows = _run_sql(
            f"SELECT revision FROM public.profiles WHERE user_id = '{user_id}'"
        )
        assert rows[0]["revision"] == 2
        assert _count("turn_requests", user_id) == 2
        assert _count("chat_logs", user_id) == 4
        assert _count("profiles", user_id) == 1
        assert _count("outbox_events", user_id) == 0
    finally:
        _cleanup_user(client, user_id)
        _close_client(client)


def test_multi_worker_different_users_progress_independently(
    supabase_url, service_role_key
):
    user_a = _uid("mw_a")
    user_b = _uid("mw_b")
    client = create_client(supabase_url, service_role_key)
    try:
        results = _run_worker_pair(supabase_url, service_role_key, user_a, user_b)
        assert [r["ok"] for r in results] == [True, True]
        # no conflicts across users: each commits on its first attempt
        assert [r["attempts"] for r in results] == [1, 1]
        for user in (user_a, user_b):
            rows = _run_sql(
                f"SELECT revision FROM public.profiles WHERE user_id = '{user}'"
            )
            assert rows[0]["revision"] == 1
            assert _count("turn_requests", user) == 1
            assert _count("profiles", user) == 1
    finally:
        for user in (user_a, user_b):
            _cleanup_user(client, user)
        _close_client(client)


def test_concurrent_first_profile_creation_single_row(supabase_url, service_role_key):
    """N workers creating the first profile concurrently produce one row."""
    user_id = _uid("mw_profile")
    workers = 4
    barrier = threading.Barrier(workers)

    def one(index: int) -> dict:
        client = create_client(supabase_url, service_role_key)
        try:
            barrier.wait(timeout=30)
            params = _commit_params(
                user_id,
                str(uuid.uuid4()),
                payload_hash=f"{index}" * 64,
                expected_revision=0,
            )
            return _call_commit(client, params)
        finally:
            _close_client(client)

    client = create_client(supabase_url, service_role_key)
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(one, i) for i in range(workers)]
            results = [f.result(timeout=60) for f in futures]
        successes = [r for r in results if "error" not in r]
        mismatches = [r["error"]["code"] for r in results if "error" in r]
        assert len(successes) == 1
        assert mismatches.count("revision_mismatch") == workers - 1
        assert _count("profiles", user_id) == 1
        rows = _run_sql(
            f"SELECT revision FROM public.profiles WHERE user_id = '{user_id}'"
        )
        assert rows[0]["revision"] == 1
    finally:
        _cleanup_user(client, user_id)
        _close_client(client)


# ---------------------------------------------------------------------------
# 5. Outbox
# ---------------------------------------------------------------------------


def test_eligible_turn_creates_exactly_one_event(service_client: Client):
    user_id = _uid("outbox_one")
    request_id = str(uuid.uuid4())
    events = [
        {
            "event_type": "archival_extraction_requested",
            "payload": {"message_id": request_id, "kind": "archival", "version": 1},
            "idempotency_key": f"archival:{request_id}:v1",
        }
    ]
    try:
        result = _call_commit(
            service_client,
            _commit_params(user_id, request_id, payload_hash="a" * 64, outbox_events=events),
        )
        assert "error" not in result
        assert len(result["outbox_events"]) == 1
        assert _count("outbox_events", user_id) == 1

        # replay (same request id) does not create a new event
        replay = _call_commit(
            service_client,
            _commit_params(user_id, request_id, payload_hash="a" * 64, outbox_events=events),
        )
        assert replay["outbox_events"] == result["outbox_events"]
        assert _count("outbox_events", user_id) == 1

        # payload carries references only: no content, no identity
        rows = _run_sql(
            "SELECT payload FROM public.outbox_events "
            f"WHERE user_id = '{user_id}'"
        )
        payload = rows[0]["payload"]
        assert set(payload) == {"message_id", "kind", "version"}
        text = json.dumps(payload)
        assert user_id not in text
        assert "Hello" not in text
        assert "Hi there" not in text
    finally:
        _cleanup_user(service_client, user_id)


def test_revision_retry_that_later_wins_creates_single_event(service_client: Client):
    user_id = _uid("outbox_retry")
    try:
        # consume revision 0 with a first turn
        first = _call_commit(
            service_client, _commit_params(user_id, str(uuid.uuid4()), payload_hash="a" * 64)
        )
        assert first["committed_revision"] == 1

        # second turn attempts stale revision 0 -> revision_mismatch (no writes)
        request_id = str(uuid.uuid4())
        events = [
            {
                "event_type": "archival_extraction_requested",
                "payload": {"message_id": request_id, "kind": "archival", "version": 1},
                "idempotency_key": f"archival:{request_id}:v1",
            }
        ]
        stale = _call_commit(
            service_client,
            _commit_params(
                user_id,
                request_id,
                payload_hash="b" * 64,
                expected_revision=0,
                outbox_events=events,
            ),
        )
        assert stale["error"]["code"] == "revision_mismatch"
        assert _count("outbox_events", user_id) == 0
        assert _count("chat_logs", user_id) == 2

        # retry with the reloaded revision 1 -> exactly one event
        ok = _call_commit(
            service_client,
            _commit_params(
                user_id,
                request_id,
                payload_hash="b" * 64,
                expected_revision=1,
                outbox_events=events,
            ),
        )
        assert "error" not in ok
        assert len(ok["outbox_events"]) == 1
        assert _count("outbox_events", user_id) == 1
        assert _count("chat_logs", user_id) == 4
    finally:
        _cleanup_user(service_client, user_id)


def test_rollback_removes_outbox_event(service_client: Client):
    user_id = _uid("outbox_rollback")
    request_id = str(uuid.uuid4())
    events = [
        {
            "event_type": "archival_extraction_requested",
            "payload": {"message_id": request_id, "kind": "archival", "version": 1},
            "idempotency_key": f"archival:{request_id}:v1",
        }
    ]
    trigger_fn = f"pt_rb_{user_id[-8:]}_fn"
    trigger_name = f"pt_rb_{user_id[-8:]}_trg"
    try:
        _run_sql(
            f"CREATE OR REPLACE FUNCTION public.{trigger_fn}() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN "
            "RAISE EXCEPTION 'injected fail'; END $$;"
        )
        _run_sql(
            f"CREATE TRIGGER {trigger_name} AFTER INSERT ON public.chat_logs "
            "FOR EACH ROW WHEN (NEW.role = 'assistant') "
            f"EXECUTE FUNCTION public.{trigger_fn}()"
        )
        with pytest.raises(APIError) as exc:
            _call_commit(
                service_client,
                _commit_params(
                    user_id, request_id, payload_hash="a" * 64, outbox_events=events
                ),
            )
        assert exc.value.code == "P0001"
        # the event was born in the same transaction: rollback removed it
        assert _count("outbox_events", user_id) == 0
        assert _count("chat_logs", user_id) == 0
        assert _count("turn_requests", user_id) == 0
    finally:
        _run_sql(f"DROP TRIGGER IF EXISTS {trigger_name} ON public.chat_logs")
        _run_sql(f"DROP FUNCTION IF EXISTS public.{trigger_fn}()")
        _cleanup_user(service_client, user_id)
