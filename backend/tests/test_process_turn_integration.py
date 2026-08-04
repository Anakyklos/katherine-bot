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

from backend.admission import AdmissionRuntimeConfig, compute_turn_correlation
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


def test_pending_request_with_expired_lease_is_stale(service_client: Client):
    """A pending reservation whose lease expired can never complete on its
    own: v1 performs NO automatic reclaim through the endpoint, so replay is
    unavailable and the client needs a new request id."""
    user_id = _uid("pending_stale")
    request_id = str(uuid.uuid4())
    try:
        _run_sql(
            "INSERT INTO public.profiles (user_id) VALUES "
            f"('{user_id}')"
        )
        _run_sql(
            "INSERT INTO public.turn_requests "
            "(user_id, request_id, payload_hash_sha256, status, lease_owner, lease_expires_at) "
            f"VALUES ('{user_id}', '{request_id}', '{'d' * 64}', 'pending', "
            "'worker-x', timezone('utc'::text, now()) - INTERVAL '1 hour')"
        )
        replay = _call_replay(service_client, user_id, request_id)
        assert replay == {"status": "request_replay_unavailable"}
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
# 4. Multi-worker consistency (independent subprocesses, real ProcessTurn)
# ---------------------------------------------------------------------------

# Each worker instantiates the REAL repositories and the REAL ProcessTurn use
# case over an INDEPENDENT Supabase client, with a deterministic fake provider
# and fake context loader (no network). Coordination happens ONLY in the
# commit path: the commit repository is wrapped by a file rendezvous on the
# FIRST commit attempt (expected_revision == 0), so both workers must have
# loaded revision 0 and entered their first commit before either commit
# proceeds. The rendezvous runs inside run_blocking_write(), which never
# abandons the operation, so the artificial wait does NOT consume the turn
# budget. The loser receives revision_mismatch, reloads state/context and
# runs its second generation; the bounded retry lives inside
# ProcessTurn.execute() — the worker never reimplements the retry loop.
_WORKER_SCRIPT = r"""
import asyncio
import json
import os
import sys
import time
import uuid

from supabase import create_client

from backend.admission import (
    AdmissionRuntimeConfig,
    compute_turn_correlation,
)
from backend.emotional_domain import AppraisalV1, TransitionConfig
from backend.process_turn import (
    ProcessTurn,
    ProcessTurnInput,
    TurnMode,
    new_lease_owner,
)
from backend.relationship import RelationshipTransitionConfig
from backend.trusted_context import LoadedContextData
from backend.turn_execution import (
    TurnExecutionConfig,
    create_budget,
)
from backend.turn_repositories import (
    PersistenceError,
    TurnCommitRepository,
    TurnReplayRepository,
    UserStateRepository,
)

url = os.environ["SUPABASE_URL"]
key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
user_id = os.environ["WORKER_USER"]
request_id = os.environ["WORKER_REQUEST"]
commit_ready_file = os.environ["WORKER_COMMIT_READY"]
commit_peer_file = os.environ["WORKER_COMMIT_PEER"]
start_ready_file = os.environ["WORKER_START_READY"]
start_peer_file = os.environ["WORKER_START_PEER"]
correlation = os.environ["WORKER_CORRELATION"]
trace_file = os.environ["WORKER_TRACE"]


def trace(event, **fields):
    with open(trace_file, "a") as f:
        f.write(
            json.dumps(
                {"event": event, "ts": round(time.time(), 3), "pid": os.getpid(), **fields}
            )
            + "\n"
        )


class CommitBarrierRepository:
    '''Real TurnCommitRepository + deterministic rendezvous on the FIRST
    commit attempt (expected_revision == 0).

    Both workers signal that they have loaded revision 0 and entered their
    first commit, then wait for each other before any commit proceeds. The
    wait runs inside run_blocking_write(), which never abandons the write,
    so it does NOT consume the turn budget (the pre-commit budget check has
    already passed). Retry commits (expected_revision > 0) never re-sync.
    '''

    def __init__(self, inner, ready_file, peer_file, timeout=60.0):
        self._inner = inner
        self._ready_file = ready_file
        self._peer_file = peer_file
        self._timeout = timeout

    def commit(self, *args, **kwargs):
        expected = kwargs.get("expected_revision")
        trace("commit_enter", expected=expected)
        if expected == 0:
            trace("barrier_wait_start")
            open(self._ready_file, "w").close()
            deadline = time.monotonic() + self._timeout
            while not os.path.exists(self._peer_file):
                if time.monotonic() > deadline:
                    # Surfaces distinctly in the worker failure reason
                    # (allowlisted: propagates as-is, never wrapped).
                    raise PersistenceError(
                        "database_error", "commit rendezvous timed out"
                    )
                time.sleep(0.02)
            trace("barrier_wait_end")
        result = self._inner.commit(*args, **kwargs)
        trace("commit_done", revision=result.committed_revision)
        return result


def wait_for_start_peer():
    '''Absorb subprocess startup skew OUTSIDE the turn budget.

    Both workers signal once imports and the client are ready, then wait for
    each other. The commit rendezvous then resolves within milliseconds
    instead of racing against startup.
    '''
    trace("start_ready")
    open(start_ready_file, "w").close()
    deadline = time.monotonic() + 60.0
    while not os.path.exists(start_peer_file):
        if time.monotonic() > deadline:
            raise RuntimeError("start_sync_timeout")
        time.sleep(0.02)
    trace("start_peer_seen")


class FakeProvider:
    '''Deterministic provider: no network, counts provider generations.'''

    def __init__(self):
        self.generations = 0
        self.appraisals = 0

    async def appraise(self, message, budget):
        self.appraisals += 1
        return AppraisalV1.neutral()

    async def generate(self, messages, budget):
        self.generations += 1
        return f"worker response {self.generations}"

    def build_trusted_policy(self, emotional_state, relationship, adaptation_strategy=""):
        return "policy"


def close_client(client):
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
    auth = getattr(client, "auth", None)
    if auth is not None and hasattr(auth, "close"):
        auth.close()


def fail(reason):
    print(json.dumps({"ok": False, "reason": reason}))
    sys.exit(1)


def main():
    client = create_client(url, key)
    try:
        wait_for_start_peer()
        asyncio.run(run(client))
    except Exception as exc:  # noqa: BLE001 - worker reports structured failure
        trace("error", error=type(exc).__name__ + ": " + str(exc))
        fail(type(exc).__name__ + ": " + str(exc))
    finally:
        # Marker files (start/commit rendezvous) are removed by the harness
        # after BOTH workers exit: a slow peer must never lose the peer marker
        # mid-flight.
        close_client(client)


async def run(client):
    try:
        client_provider = lambda: client

        class TracingStateRepository:
            def __init__(self, inner):
                self._inner = inner

            def load(self, *args, **kwargs):
                state = self._inner.load(*args, **kwargs)
                trace("state_loaded", revision=state.revision)
                return state

        state_repo = TracingStateRepository(UserStateRepository(client_provider))
        commit_repo = CommitBarrierRepository(
            TurnCommitRepository(client_provider), commit_ready_file, commit_peer_file
        )
        replay_repo = TurnReplayRepository(client_provider)
        context_loader = lambda uid, message, user_state: LoadedContextData()
        provider = FakeProvider()
        config = TurnExecutionConfig.defaults()
        use_case = ProcessTurn(
            state_repository=state_repo,
            commit_repository=commit_repo,
            replay_repository=replay_repo,
            context_loader=context_loader,
            provider=provider,
            transition_config=TransitionConfig.defaults(),
            relationship_config=RelationshipTransitionConfig.defaults(),
            clock=time.time,
            # Coherent with the same config that drives the budget; the
            # rendezvous never counts against it (write path is never
            # abandoned).
            supabase_timeout=config.supabase_timeout,
            archival_extraction_enabled=False,
            # Per-instance lease owner: this worker NEVER shares an owner.
            lease_owner=new_lease_owner(),
        )
        inp = ProcessTurnInput(
            authenticated_user_id=user_id,
            request_id=request_id,
            user_message="Hello",
            budget=create_budget(config),
            correlation=correlation,
            mode=TurnMode.normal,
        )
        result = await use_case.execute(inp)
        print(
            json.dumps(
                {
                    "ok": True,
                    "generations": provider.generations,
                    "committed_revision": result.committed.committed_revision,
                    "response": result.response,
                    "request_id": result.committed.request_id,
                    "user_message_id": result.committed.user_message_id,
                    "assistant_message_id": result.committed.assistant_message_id,
                }
            )
        )
    except Exception as exc:  # noqa: BLE001 - worker reports structured failure
        fail(type(exc).__name__ + ": " + str(exc))


main()
"""


def _read_trace_events(path: str) -> list[dict]:
    """Parse a worker trace file (one JSON event per line) into a list."""
    events: list[dict] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def _trace_field_values(events: list[dict], event: str, field: str) -> list:
    return [ev[field] for ev in events if ev["event"] == event]


def _run_worker_pair(
    url: str, key: str, user_a: str, user_b: str
) -> tuple[list[dict], tuple[list[dict], list[dict]]]:
    """Run two independent subprocess workers synchronized by a file barrier.

    Returns ``(results, traces)`` where ``results`` is the parsed stdout of
    each worker and ``traces`` the parsed structured trace events of each
    worker (captured before the rendezvous directory is removed).
    """
    ready_dir = f"/tmp/opencode/pt_workers_{uuid.uuid4().hex}"
    os.makedirs(ready_dir, exist_ok=True)
    admission_config = AdmissionRuntimeConfig.from_values("s" * 32)

    def spawn(user: str, index: int) -> tuple[subprocess.Popen, str, str, str, str]:
        request_id = str(uuid.uuid4())
        commit_ready = os.path.join(ready_dir, f"commit_ready_{index}")
        commit_peer = os.path.join(ready_dir, f"commit_ready_{1 - index}")
        start_ready = os.path.join(ready_dir, f"start_ready_{index}")
        start_peer = os.path.join(ready_dir, f"start_ready_{1 - index}")
        trace_file = os.path.join(ready_dir, f"trace_{index}.jsonl")
        env = dict(os.environ)
        env.update(
            SUPABASE_URL=url,
            SUPABASE_SERVICE_ROLE_KEY=key,
            WORKER_USER=user,
            WORKER_REQUEST=request_id,
            WORKER_COMMIT_READY=commit_ready,
            WORKER_COMMIT_PEER=commit_peer,
            WORKER_START_READY=start_ready,
            WORKER_START_PEER=start_peer,
            WORKER_TRACE=trace_file,
            WORKER_CORRELATION=compute_turn_correlation(admission_config, request_id),
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", _WORKER_SCRIPT],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return proc, commit_ready, request_id, user, trace_file

    # Profiles pre-exist at revision 0 with valid neutral snapshots BEFORE
    # either worker spawns: both deterministically load revision 0 and enter
    # their first commit with expected_revision == 0. (Concurrent
    # first-profile creation is covered by a dedicated test.)
    _run_sql(
        "INSERT INTO public.profiles (user_id, revision, emotional_state, relationship_state) VALUES "
        f"('{user_a}', 0, '{json.dumps(_emotional_state())}'::jsonb, '{json.dumps(_relationship_state())}'::jsonb), "
        f"('{user_b}', 0, '{json.dumps(_emotional_state())}'::jsonb, '{json.dumps(_relationship_state())}'::jsonb) "
        "ON CONFLICT (user_id) DO NOTHING"
    )
    proc_a, ready_a, request_a, user_a_final, trace_a = spawn(user_a, 0)
    proc_b, ready_b, request_b, _, trace_b = spawn(user_b, 1)
    assert ready_a != ready_b
    try:
        out_a, err_a = proc_a.communicate(timeout=90)
        out_b, err_b = proc_b.communicate(timeout=90)
        # Capture trace contents BEFORE the rendezvous directory is removed.
        events_a = _read_trace_events(trace_a)
        events_b = _read_trace_events(trace_b)
    finally:
        proc_a.kill()
        proc_b.kill()
        for f in (
            ready_a,
            ready_b,
            os.path.join(ready_dir, "start_ready_0"),
            os.path.join(ready_dir, "start_ready_1"),
        ):
            try:
                os.unlink(f)
            except OSError:
                pass
        try:
            os.rmdir(ready_dir)
        except OSError:
            pass
    if proc_a.returncode != 0 or proc_b.returncode != 0:
        for label, trace_path in (("A", trace_a), ("B", trace_b)):
            try:
                print(f"--- worker {label} trace ---")
                print(open(trace_path).read())
            except OSError:
                print(f"--- worker {label} trace: missing ---")
    assert proc_a.returncode == 0, f"worker A failed: {err_a} | out: {out_a}"
    assert proc_b.returncode == 0, f"worker B failed: {err_b} | out: {out_b}"
    return [
        json.loads(out_a),
        json.loads(out_b),
    ], (events_a, events_b)


def test_multi_worker_same_user_cas_consistency(
    supabase_url, service_role_key, service_client
):
    """Two independent ProcessTurn workers race the same revision: exactly one
    commit wins the first attempt, the loser receives revision_mismatch,
    reloads state/context and runs its second generation."""
    user_id = _uid("mw_same")
    client = create_client(supabase_url, service_role_key)
    try:
        results, (events_a, events_b) = _run_worker_pair(
            supabase_url, service_role_key, user_id, user_id
        )
        assert [r["ok"] for r in results] == [True, True]
        # exactly one worker needed a second generation; the other won on the
        # first attempt (order between workers is nondeterministic)
        assert sorted(r["generations"] for r in results) == [1, 2]
        assert sorted(r["committed_revision"] for r in results) == [1, 2]
        # each worker returned the CommittedTurn the database persisted
        for result in results:
            assert result["response"].startswith("worker response ")
            assert result["user_message_id"] == result["request_id"]
            assert len(result["assistant_message_id"]) == 36
        # trace proof: both workers loaded revision 0 and entered their first
        # commit with expected_revision == 0 (the rendezvous); the loser then
        # reloaded revision 1 and committed expected_revision == 1 exactly
        # once. No third attempt exists: commit_enter count == generations.
        initial_revisions = sorted(
            _trace_field_values(events, "state_loaded", "revision")[0]
            for events in (events_a, events_b)
        )
        assert initial_revisions == [0, 0]
        first_commits = sorted(
            _trace_field_values(events, "commit_enter", "expected")[0]
            for events in (events_a, events_b)
        )
        assert first_commits == [0, 0]
        commit_sequences = sorted(
            _trace_field_values(events, "commit_enter", "expected")
            for events in (events_a, events_b)
        )
        assert commit_sequences == [[0], [0, 1]]
        for events, result in zip((events_a, events_b), results):
            assert len(_trace_field_values(events, "commit_enter", "expected")) == (
                result["generations"]
            )
        # the revision increased exactly twice (one per committed request)
        rows = _run_sql(
            f"SELECT revision FROM public.profiles WHERE user_id = '{user_id}'"
        )
        assert rows[0]["revision"] == 2
        assert _count("turn_requests", user_id) == 2
        assert _count("chat_logs", user_id) == 4
        assert _count("profiles", user_id) == 1
        assert _count("outbox_events", user_id) == 0
        # persisted results are exactly what the workers returned: replay of
        # each committed request returns the same response
        for result in results:
            replay = _call_replay(service_client, user_id, result["request_id"])
            assert replay["replay_payload"]["response"] == result["response"]
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
        results, (events_a, events_b) = _run_worker_pair(
            supabase_url, service_role_key, user_a, user_b
        )
        assert [r["ok"] for r in results] == [True, True]
        # no conflicts across users: each commits on its first attempt,
        # proving there is no global lock between workers
        assert [r["generations"] for r in results] == [1, 1]
        # trace proof: each worker loaded revision 0 and entered exactly one
        # commit with expected_revision == 0, committing revision 0 -> 1
        for events in (events_a, events_b):
            assert _trace_field_values(events, "state_loaded", "revision") == [0]
            assert _trace_field_values(events, "commit_enter", "expected") == [0]
            assert _trace_field_values(events, "commit_done", "revision") == [1]
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
