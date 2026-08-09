"""Unit tests for the #316 operational data retention runner and policy.

Covers the mandatory scenarios of issue #316 without any real network:

 1.  admission_reservation older than 24h is removed.
 2.  admission_reservation at or inside 24h stays.
 3.  Cleanup cannot bypass current quota via delete_history (the admission
     ledger inside the horizon is never touched, and the runner never calls
     any RPC other than the three purge functions).
 4.  outbox_events ``completed`` with expired ``retention_until`` is removed.
 5.  outbox_events ``dead_letter`` with expired ``retention_until`` is removed.
 6.  ``pending``/``processing``/``failed`` are never purged by age.
 7.  A final event whose ``retention_until`` has not expired stays.
 8.  Privacy operation ledger inside 30 days stays.
 9.  Privacy operation ledger expired is removed.
10.  Batch size is really limited (per-statement and per-round caps).
11.  A second run without new eligible rows is idempotent.
12.  Two concurrent runs do not corrupt state or double-process rows.
13.  Cleanup never touches user A/B content outside the rules (the runner
     only invokes the three operational purge RPCs).
14.  No log line contains user_id, HMAC, content, raw SQL or secrets.
15.  The ``--once`` command runs with injected dependencies, no network.
16.  Policy/registry is versioned and config validation fails closed.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import subprocess
import sys
import textwrap
import threading
import time
from collections import defaultdict
from types import SimpleNamespace

import pytest

from backend.atomic_turn_commit import PersistenceError
from backend.observability import EVENT_NAMES
from backend.retention import (
    DEFAULT_RETENTION_BATCH_SIZE,
    RetentionCategory,
    RetentionConfig,
    RetentionRunResult,
    RetentionRunner,
    SupabaseRetentionRepository,
)
from backend.retention_cli import (
    RetentionRuntimeConfig,
    RetentionRuntimeConfigurationError,
    main as cli_main,
)
from backend.retention_policy import (
    OUTBOX_ACTIVE_STATUSES,
    OUTBOX_FINAL_STATUSES,
    RETENTION_MAX_BATCH_SIZE,
    RETENTION_POLICY_SCHEMA_VERSION,
    RetentionPolicy,
    compute_cutoff,
    default_retention_policy,
    retention_cutoffs,
)
from backend.turn_execution import TurnExecutionConfig

NOW = 1700000000.0  # 2023-11-14T22:13:20+00:00
HOUR = 3600.0
DAY = 86400.0


def _iso(epoch: float) -> str:
    return _dt.datetime.fromtimestamp(epoch, tz=_dt.timezone.utc).isoformat()


# ─── In-memory repository simulating the SQL purge contract ─────────────────


class FakeRetentionRepository:
    """Simulates the SQL purge boundary.

    Eligibility store: for each category, a list of (row_id, timestamp,
    status). Purge removes rows whose timestamp is BEFORE the cutoff
    (admission/privacy) or, for outbox, whose status is final AND
    ``retention_until`` is BEFORE the cutoff. Records every call.
    """

    def __init__(self, rows: dict[str, list[tuple]]):
        self._rows: dict[str, list[tuple]] = {
            category: list(entries) for category, entries in rows.items()
        }
        self.calls: list[tuple[str, str, int]] = []

    def purge_admission_reservations(self, cutoff: str, batch_size: int) -> int:
        return self._purge(
            RetentionCategory.ADMISSION_RESERVATIONS.value, cutoff, batch_size
        )

    def purge_privacy_operations(self, cutoff: str, batch_size: int) -> int:
        return self._purge(
            RetentionCategory.PRIVACY_OPERATIONS.value, cutoff, batch_size
        )

    def purge_outbox_events(self, cutoff: str, batch_size: int) -> int:
        return self._purge(RetentionCategory.OUTBOX_EVENTS.value, cutoff, batch_size)

    def _purge(self, category: str, cutoff: str, batch_size: int) -> int:
        self.calls.append((category, cutoff, batch_size))
        cutoff_dt = _dt.datetime.fromisoformat(cutoff)
        eligible: list[tuple] = []
        for entry in self._rows[category]:
            row_id, timestamp, status = entry
            if category == RetentionCategory.OUTBOX_EVENTS.value:
                # Outbox: only final states whose retention_until is past.
                if status in OUTBOX_FINAL_STATUSES and timestamp < cutoff_dt:
                    eligible.append(entry)
            else:
                # Admission/privacy: row timestamp strictly before cutoff.
                if timestamp < cutoff_dt:
                    eligible.append(entry)
        batch = eligible[:batch_size]
        for entry in batch:
            self._rows[category].remove(entry)
        return len(batch)

    def remaining(self, category: str) -> list[tuple]:
        return list(self._rows[category])


def _runner(repo, *, clock=NOW, config=None):
    return RetentionRunner(
        repository=repo,
        clock=lambda: clock,
        config=config if config is not None else RetentionConfig(batch_size=10),
        turn_config=TurnExecutionConfig.defaults(),
    )


# ─── 1/2. admission_reservations horizon ────────────────────────────────────


def test_admission_older_than_24h_is_removed_current_stays():
    repo = FakeRetentionRepository(
        {
            RetentionCategory.ADMISSION_RESERVATIONS.value: [
                ("expired", _dt.datetime.fromtimestamp(NOW - 25 * HOUR, tz=_dt.timezone.utc), "row"),
                ("current", _dt.datetime.fromtimestamp(NOW - 23 * HOUR, tz=_dt.timezone.utc), "row"),
            ],
            RetentionCategory.PRIVACY_OPERATIONS.value: [],
            RetentionCategory.OUTBOX_EVENTS.value: [],
        }
    )
    result = asyncio.run(_runner(repo).run_once())
    admission = result.results[RetentionCategory.ADMISSION_RESERVATIONS.value]
    assert admission.purged == 1
    remaining = repo.remaining(RetentionCategory.ADMISSION_RESERVATIONS.value)
    assert [entry[0] for entry in remaining] == ["current"]
    # The cutoff passed to the RPC is exactly now - 24h.
    admission_calls = [
        c for c in repo.calls if c[0] == RetentionCategory.ADMISSION_RESERVATIONS.value
    ]
    assert admission_calls
    assert _dt.datetime.fromisoformat(admission_calls[0][1]) == _dt.datetime.fromtimestamp(
        NOW - 24 * HOUR, tz=_dt.timezone.utc
    )


def test_policy_computes_correct_24h_cutoff():
    cutoff = compute_cutoff(NOW, default_retention_policy().admission_reservations_horizon)
    assert cutoff == _dt.datetime.fromtimestamp(NOW - 24 * HOUR, tz=_dt.timezone.utc)


# ─── 3. Quota ledger cannot be bypassed ─────────────────────────────────────


def test_cleanup_never_touches_current_quota_rows():
    """Rows inside the 24h horizon (the current quota window) stay; the
    runner never invokes anything but the three purge RPCs, so cleanup can
    never act as a delete_history-style quota reset."""
    repo = FakeRetentionRepository(
        {
            RetentionCategory.ADMISSION_RESERVATIONS.value: [
                ("recent", _dt.datetime.fromtimestamp(NOW - 1 * HOUR, tz=_dt.timezone.utc), "row"),
            ],
            RetentionCategory.PRIVACY_OPERATIONS.value: [],
            RetentionCategory.OUTBOX_EVENTS.value: [],
        }
    )
    result = asyncio.run(_runner(repo).run_once())
    assert repo.remaining(RetentionCategory.ADMISSION_RESERVATIONS.value) != []
    assert result.results[RetentionCategory.ADMISSION_RESERVATIONS.value].purged == 0
    # Only the three purge RPC names may ever be invoked.
    rpc_names = {c[0] for c in repo.calls}
    assert rpc_names == {
        RetentionCategory.ADMISSION_RESERVATIONS.value,
        RetentionCategory.PRIVACY_OPERATIONS.value,
        RetentionCategory.OUTBOX_EVENTS.value,
    }
    assert "delete_history" not in rpc_names


# ─── 4/5/6/7. outbox_events eligibility ─────────────────────────────────────


def test_outbox_completed_and_dead_letter_expired_are_removed_active_stay():
    now_dt = _dt.datetime.fromtimestamp(NOW, tz=_dt.timezone.utc)
    repo = FakeRetentionRepository(
        {
            RetentionCategory.ADMISSION_RESERVATIONS.value: [],
            RetentionCategory.PRIVACY_OPERATIONS.value: [],
            RetentionCategory.OUTBOX_EVENTS.value: [
                ("completed-expired", now_dt - _dt.timedelta(hours=2), "completed"),
                ("dead-expired", now_dt - _dt.timedelta(hours=3), "dead_letter"),
                ("completed-future", now_dt + _dt.timedelta(hours=1), "completed"),
                ("pending-old", now_dt - _dt.timedelta(days=40), "pending"),
                ("processing-old", now_dt - _dt.timedelta(days=40), "processing"),
                ("failed-old", now_dt - _dt.timedelta(days=40), "failed"),
            ],
        }
    )
    result = asyncio.run(_runner(repo).run_once())
    outbox = result.results[RetentionCategory.OUTBOX_EVENTS.value]
    assert outbox.purged == 2
    remaining = {
        entry[0]: entry[2] for entry in repo.remaining(RetentionCategory.OUTBOX_EVENTS.value)
    }
    assert remaining == {
        "completed-future": "completed",
        "pending-old": "pending",
        "processing-old": "processing",
        "failed-old": "failed",
    }


def test_outbox_active_states_never_purged_by_age():
    """pending/processing/failed are never eligible, regardless of age."""
    assert OUTBOX_FINAL_STATUSES == {"completed", "dead_letter"}
    assert OUTBOX_ACTIVE_STATUSES == {"pending", "processing", "failed"}
    assert OUTBOX_ACTIVE_STATUSES.isdisjoint(OUTBOX_FINAL_STATUSES)


def test_outbox_cutoff_is_now():
    cutoffs = retention_cutoffs(NOW)
    assert _dt.datetime.fromisoformat(
        cutoffs[RetentionCategory.OUTBOX_EVENTS.value]
    ) == _dt.datetime.fromtimestamp(NOW, tz=_dt.timezone.utc)


# ─── 8/9. privacy_operations horizon ────────────────────────────────────────


def test_privacy_ledger_inside_30_days_stays_expired_removed():
    repo = FakeRetentionRepository(
        {
            RetentionCategory.ADMISSION_RESERVATIONS.value: [],
            RetentionCategory.PRIVACY_OPERATIONS.value: [
                ("expired", _dt.datetime.fromtimestamp(NOW - 31 * DAY, tz=_dt.timezone.utc), "row"),
                ("current", _dt.datetime.fromtimestamp(NOW - 29 * DAY, tz=_dt.timezone.utc), "row"),
            ],
            RetentionCategory.OUTBOX_EVENTS.value: [],
        }
    )
    result = asyncio.run(_runner(repo).run_once())
    privacy = result.results[RetentionCategory.PRIVACY_OPERATIONS.value]
    assert privacy.purged == 1
    remaining = repo.remaining(RetentionCategory.PRIVACY_OPERATIONS.value)
    assert [entry[0] for entry in remaining] == ["current"]


# ─── 10. Batch size is really limited ───────────────────────────────────────


def test_batch_size_is_limited_per_statement_and_round():
    rows = [
        (f"row-{i}", _dt.datetime.fromtimestamp(NOW - 2 * DAY, tz=_dt.timezone.utc), "row")
        for i in range(10)
    ]
    repo = FakeRetentionRepository(
        {
            RetentionCategory.ADMISSION_RESERVATIONS.value: rows,
            RetentionCategory.PRIVACY_OPERATIONS.value: [],
            RetentionCategory.OUTBOX_EVENTS.value: [],
        }
    )
    config = RetentionConfig(batch_size=3, max_rows_per_category=10)
    result = asyncio.run(_runner(repo, config=config).run_once())
    admission = result.results[RetentionCategory.ADMISSION_RESERVATIONS.value]
    assert admission.purged == 10
    assert admission.batches == 4  # 3 + 3 + 3 + 1
    admission_calls = [
        c for c in repo.calls if c[0] == RetentionCategory.ADMISSION_RESERVATIONS.value
    ]
    assert all(c[2] == 3 for c in admission_calls), "batch_size must never exceed 3"


def test_batch_size_is_capped_at_sql_maximum():
    with pytest.raises(ValueError):
        RetentionConfig(batch_size=RETENTION_MAX_BATCH_SIZE + 1)
    with pytest.raises(ValueError):
        RetentionConfig(batch_size=0)
    with pytest.raises(ValueError):
        RetentionConfig(batch_size=True)


def test_per_category_cap_bounds_a_round():
    rows = [
        (f"row-{i}", _dt.datetime.fromtimestamp(NOW - 2 * DAY, tz=_dt.timezone.utc), "row")
        for i in range(10)
    ]
    repo = FakeRetentionRepository(
        {
            RetentionCategory.ADMISSION_RESERVATIONS.value: rows,
            RetentionCategory.PRIVACY_OPERATIONS.value: [],
            RetentionCategory.OUTBOX_EVENTS.value: [],
        }
    )
    config = RetentionConfig(batch_size=3, max_rows_per_category=6)
    result = asyncio.run(_runner(repo, config=config).run_once())
    admission = result.results[RetentionCategory.ADMISSION_RESERVATIONS.value]
    assert admission.purged == 6, "round must stop at the per-category cap"
    assert admission.batches == 2
    # Remaining rows stay for the next round.
    assert len(repo.remaining(RetentionCategory.ADMISSION_RESERVATIONS.value)) == 4


# ─── 11. Idempotent second run ──────────────────────────────────────────────


def test_second_run_without_new_eligible_rows_is_idempotent():
    repo = FakeRetentionRepository(
        {
            RetentionCategory.ADMISSION_RESERVATIONS.value: [
                ("expired", _dt.datetime.fromtimestamp(NOW - 2 * DAY, tz=_dt.timezone.utc), "row"),
            ],
            RetentionCategory.PRIVACY_OPERATIONS.value: [],
            RetentionCategory.OUTBOX_EVENTS.value: [],
        }
    )
    runner = _runner(repo)
    first = asyncio.run(runner.run_once())
    second = asyncio.run(runner.run_once())
    assert first.total_purged == 1
    assert second.total_purged == 0
    assert second.results[RetentionCategory.ADMISSION_RESERVATIONS.value].batches == 1
    assert repo.remaining(RetentionCategory.ADMISSION_RESERVATIONS.value) == []


# ─── 12. Concurrent runs are safe ───────────────────────────────────────────


def test_concurrent_runs_do_not_double_process():
    rows = [
        (f"row-{i}", _dt.datetime.fromtimestamp(NOW - 2 * DAY, tz=_dt.timezone.utc), "row")
        for i in range(50)
    ]
    repo = FakeRetentionRepository(
        {
            RetentionCategory.ADMISSION_RESERVATIONS.value: rows,
            RetentionCategory.PRIVACY_OPERATIONS.value: [],
            RetentionCategory.OUTBOX_EVENTS.value: [],
        }
    )
    runner = _runner(repo, config=RetentionConfig(batch_size=7, max_rows_per_category=100))

    async def _scenario():
        first = asyncio.create_task(runner.run_once())
        second = asyncio.create_task(runner.run_once())
        results = await asyncio.gather(first, second)
        return results

    results = asyncio.run(_scenario())
    total_purged = sum(r.total_purged for r in results)
    # Every eligible row is deleted exactly once across both runs.
    assert total_purged == 50
    assert repo.remaining(RetentionCategory.ADMISSION_RESERVATIONS.value) == []


def test_concurrent_runs_via_threads_are_safe():
    """Two runner coroutines on separate event loops (threads) against one
    shared repository: no corruption, every row processed exactly once."""
    rows = [
        (f"row-{i}", _dt.datetime.fromtimestamp(NOW - 2 * DAY, tz=_dt.timezone.utc), "row")
        for i in range(20)
    ]
    repo = FakeRetentionRepository(
        {
            RetentionCategory.ADMISSION_RESERVATIONS.value: rows,
            RetentionCategory.PRIVACY_OPERATIONS.value: [],
            RetentionCategory.OUTBOX_EVENTS.value: [],
        }
    )
    outcomes: list[int] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def _run():
        runner = _runner(repo, config=RetentionConfig(batch_size=5, max_rows_per_category=100))
        try:
            result = asyncio.run(runner.run_once())
            with lock:
                outcomes.append(result.total_purged)
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=_run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == []
    assert sum(outcomes) == 20
    assert repo.remaining(RetentionCategory.ADMISSION_RESERVATIONS.value) == []


# ─── 13. User A/B content is never touched ──────────────────────────────────


def test_cleanup_never_touches_user_content_tables():
    """The runner only invokes the three operational purge RPCs; user
    content tables (chat_logs, memories, profiles, turn_requests,
    archival_extractions) are never named or touched."""
    repo = FakeRetentionRepository(
        {
            RetentionCategory.ADMISSION_RESERVATIONS.value: [],
            RetentionCategory.PRIVACY_OPERATIONS.value: [],
            RetentionCategory.OUTBOX_EVENTS.value: [],
        }
    )
    asyncio.run(_runner(repo).run_once())
    assert {c[0] for c in repo.calls} == {
        "admission_reservations",
        "privacy_operations",
        "outbox_events",
    }


# ─── 14. Sanitized logs ─────────────────────────────────────────────────────


def test_logs_never_contain_identifiers_content_or_secrets(caplog):
    repo = FakeRetentionRepository(
        {
            RetentionCategory.ADMISSION_RESERVATIONS.value: [
                ("SENTINEL-ROW-USER", _dt.datetime.fromtimestamp(NOW - 2 * DAY, tz=_dt.timezone.utc), "row"),
            ],
            RetentionCategory.PRIVACY_OPERATIONS.value: [],
            RetentionCategory.OUTBOX_EVENTS.value: [],
        }
    )
    runner = _runner(repo)
    with caplog.at_level(logging.INFO):
        asyncio.run(runner.run_once())
    sentinels = (
        "SENTINEL-ROW-USER",
        "HMAC-SENTINEL",
        "SENTINEL-CONTENT",
        "DELETE FROM",
        "SELECT",
        "SECRET-SENTINEL",
        "bearer",
    )
    for sentinel in sentinels:
        assert sentinel not in caplog.text
    assert "event=retention_completed" in caplog.text
    assert "event=retention_failed" not in caplog.text


def test_failure_log_is_sanitized(caplog):
    class ExplodingRepo:
        def purge_admission_reservations(self, cutoff, batch_size):
            raise PersistenceError("database_error", "persistence error")

        def purge_privacy_operations(self, cutoff, batch_size):
            raise AssertionError("must not be reached")

        def purge_outbox_events(self, cutoff, batch_size):
            raise AssertionError("must not be reached")

    runner = _runner(ExplodingRepo())
    with caplog.at_level(logging.INFO):
        with pytest.raises(PersistenceError):
            asyncio.run(runner.run_once())
    assert "event=retention_failed" in caplog.text
    assert "database_error" not in caplog.text or "persistence error" not in caplog.text
    assert "SECRET" not in caplog.text


# ─── 15. CLI --once with injected dependencies, no network ──────────────────


class _StubRunner:
    def __init__(self, result):
        self._result = result
        self.called = 0

    async def run_once(self):
        self.called += 1
        return self._result


def _stub_result(total=3):
    admission = SimpleNamespace(category="admission_reservations", purged=1, batches=1)
    privacy = SimpleNamespace(category="privacy_operations", purged=2, batches=1)
    outbox = SimpleNamespace(category="outbox_events", purged=0, batches=1)
    return SimpleNamespace(
        schema_version=RETENTION_POLICY_SCHEMA_VERSION,
        results={
            "admission_reservations": admission,
            "privacy_operations": privacy,
            "outbox_events": outbox,
        },
        total_purged=total,
    )


def test_cli_once_runs_with_injected_dependencies(capsys):
    stub = _StubRunner(_stub_result())

    def factory(config):
        return stub

    env = {
        "SUPABASE_URL": "http://127.0.0.1:54321",
        "SUPABASE_SERVICE_ROLE_KEY": "ci-test-key",
        "TURN_SUPABASE_TIMEOUT": "5",
    }
    exit_code = cli_main(["--once"], env=env, runner_factory=factory)
    assert exit_code == 0
    assert stub.called == 1
    out = capsys.readouterr().out
    assert "retention_round schema_version=1" in out
    assert "category=admission_reservations purged=1 batches=1" in out
    assert "total_purged=3" in out
    # Never printed: URL, key, identifiers.
    assert "54321" not in out
    assert "ci-test-key" not in out


def test_cli_fails_closed_without_supabase_configuration():
    exit_code = cli_main(["--once"], env={})
    assert exit_code == 1
    with pytest.raises(RetentionRuntimeConfigurationError):
        RetentionRuntimeConfig.from_env({})


def test_cli_requires_once_flag():
    with pytest.raises(SystemExit):
        cli_main([], env={})


def test_cli_failure_returns_nonzero_without_exception_text(capsys):
    def factory(config):
        class Boom:
            async def run_once(self):
                raise PersistenceError("database_error", "persistence error")

        return Boom()

    env = {
        "SUPABASE_URL": "http://127.0.0.1:54321",
        "SUPABASE_SERVICE_ROLE_KEY": "ci-test-key",
    }
    exit_code = cli_main(["--once"], env=env, runner_factory=factory)
    assert exit_code == 1
    out = capsys.readouterr().out
    assert "persistence error" not in out
    assert "SECRET" not in out


# ─── 16. Versioned policy / config fail-closed ──────────────────────────────


def test_policy_is_versioned():
    assert default_retention_policy().schema_version == RETENTION_POLICY_SCHEMA_VERSION
    assert RetentionPolicy().admission_reservations_horizon == _dt.timedelta(hours=24)
    assert RetentionPolicy().privacy_operations_horizon == _dt.timedelta(days=30)


def test_retention_events_registered_in_observability():
    assert "retention_completed" in EVENT_NAMES
    assert "retention_failed" in EVENT_NAMES


# ─── Repository adapter: fail-closed response shapes ────────────────────────


def _repo_with_response(response) -> SupabaseRetentionRepository:
    client = SimpleNamespace(
        rpc=lambda name, params: SimpleNamespace(execute=lambda: response)
    )
    return SupabaseRetentionRepository(client)


def test_repository_accepts_scalar_mapping_response():
    repo = _repo_with_response(SimpleNamespace(data={"purge_admission_reservations": 5}))
    assert repo.purge_admission_reservations("2023-01-01T00:00:00+00:00", 10) == 5


def test_repository_accepts_single_element_list_response():
    repo = _repo_with_response(
        SimpleNamespace(data=[{"purge_outbox_events": 3}])
    )
    assert repo.purge_outbox_events("2023-01-01T00:00:00+00:00", 10) == 3


def test_repository_accepts_alternative_scalar_shape():
    repo = _repo_with_response(SimpleNamespace(data={"purged": 7}))
    assert repo.purge_privacy_operations("2023-01-01T00:00:00+00:00", 10) == 7


def test_repository_accepts_bare_integer_scalar_response():
    """PostgREST returns a bare integer for scalar-returning RPCs."""
    repo = _repo_with_response(SimpleNamespace(data=4))
    assert repo.purge_admission_reservations("2023-01-01T00:00:00+00:00", 10) == 4


@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(data=[]),
        SimpleNamespace(data=[{"a": 1}, {"b": 2}]),
        SimpleNamespace(data="not-a-mapping"),
        SimpleNamespace(data=None),
        SimpleNamespace(data={"purge_admission_reservations": -1}),
        SimpleNamespace(data={"purge_admission_reservations": True}),
        SimpleNamespace(data={"purge_admission_reservations": "5"}),
        SimpleNamespace(data=-1),
        SimpleNamespace(data=True),
        SimpleNamespace(data="5"),
        SimpleNamespace(),  # missing data attribute
        "not-a-response",
        None,
    ],
)
def test_repository_maps_malformed_response_shapes_to_persistence_error(response):
    repo = _repo_with_response(response)
    with pytest.raises(PersistenceError) as exc:
        repo.purge_admission_reservations("2023-01-01T00:00:00+00:00", 10)
    assert exc.value.code == "database_error"
    assert "persistence error" in str(exc.value)


def test_repository_maps_upstream_exception_to_sanitized_persistence_error():
    class ExplodingClient:
        def rpc(self, name, params):
            raise RuntimeError("SENTINEL-UPSTREAM-SECRET")

    repo = SupabaseRetentionRepository(ExplodingClient())
    with pytest.raises(PersistenceError) as exc:
        repo.purge_admission_reservations("2023-01-01T00:00:00+00:00", 10)
    assert "SENTINEL-UPSTREAM-SECRET" not in str(exc.value)


def test_repository_without_client_raises_sanitized_error():
    repo = SupabaseRetentionRepository(None)
    with pytest.raises(PersistenceError):
        repo.purge_admission_reservations("2023-01-01T00:00:00+00:00", 10)


# ─── Pure importability ──────────────────────────────────────────────────────

_PURITY_SCRIPT = textwrap.dedent(
    """
    import sys
    import threading

    import socket as _socket

    def _forbid(*args, **kwargs):
        raise AssertionError("network socket usage during import")

    _socket.socket.connect = _forbid
    _socket.socket.connect_ex = _forbid
    _socket.create_connection = _forbid

    import supabase as _supabase

    def _no_supabase_client(*args, **kwargs):
        raise AssertionError("real Supabase client constructed during import")

    _supabase.create_client = _no_supabase_client

    threads_before = len(threading.enumerate())

    import backend.retention_policy
    import backend.retention

    threads_after = len(threading.enumerate())

    assert threads_after == threads_before, "import started a thread"
    print("RETENTION_PURITY_OK")
    """
)


def test_retention_modules_import_are_pure():
    result = subprocess.run(
        [sys.executable, "-c", _PURITY_SCRIPT],
        capture_output=True,
        text=True,
        timeout=120,
        env={"PYTHONPATH": ".", "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
    assert "RETENTION_PURITY_OK" in result.stdout
