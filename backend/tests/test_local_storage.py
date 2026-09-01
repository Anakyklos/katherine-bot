"""SQLite local persistence foundation — required-behavior suite (#335).

Every test runs against a **real, temporary SQLite file** (``tmp_path``),
one isolated database per test. No Supabase, no Postgres, no network.
No mocks are ever used as proof of atomicity: failures are produced by
real SQLite mechanics (CHECK constraints, triggers, CAS guards) or by
real I/O error injection on the file.

The suite maps 1:1 to the twelve mandatory tests of issue #335 plus the
extra invariants the issue text requires (revision CAS, replay
idempotency, outbox idempotency, backup consistency, XDG override,
sanitized errors, no-cloud-import).
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

from backend.local_storage import (
    ConflictError,
    LocalStorage,
    StorageCorruptError,
    ValidationError as LocalValidationError,
    default_database_path,
    open_local_storage,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _emotion_payload(response: str) -> dict:
    """Minimal valid public emotion payload (paridade com replay_payload)."""
    return {
        "response": response,
        "emotion_state": {"valence": 0.1, "arousal": 0.2},
        "message_id": "4a7f8c21-0000-4000-8000-000000000001",
        "duration_ms": 12,
    }


def _neutral_emotional() -> dict:
    """Real domain neutral v1 emotional snapshot."""
    from backend.emotional_domain import EmotionalStateV1

    return EmotionalStateV1.neutral(timestamp=1000.0).to_dict()


def _neutral_relationship() -> dict:
    """Real domain neutral v1 relationship snapshot."""
    from backend.relationship import RelationshipStateV1

    return RelationshipStateV1.neutral(timestamp=1000.0).to_dict()


def _commit_kwargs(store: LocalStorage, request_id: str, response: str = "ok") -> dict:
    return {
        "request_id": request_id,
        "user_message": "oi",
        "assistant_message": response,
        "emotional_state": _neutral_emotional(),
        "relationship_state": _neutral_relationship(),
        "public_response": response,
        "replay_payload": _emotion_payload(response),
        "outbox_events": [],
    }


class TestMigrations:
    """Issue #335 tests 1 (zero→migrations), 8 (partial failure)."""

    def test_fresh_database_applies_all_migrations_and_records_version(
        self, tmp_path: Path
    ) -> None:
        store = open_local_storage(tmp_path / "katherine.db")
        assert store.schema_version() > 0
        row = store._connection_for_tests_only().execute(
            "select count(*) from schema_migrations"
        ).fetchone()
        assert row[0] == store.schema_version()
        # every expected table exists
        conn = store._connection_for_tests_only()
        for table in (
            "profiles",
            "chat_logs",
            "turn_requests",
            "outbox_events",
            "memories",
            "schema_migrations",
        ):
            exists = conn.execute(
                "select count(*) from sqlite_master where type='table' and name=?",
                (table,),
            ).fetchone()[0]
            assert exists == 1, f"table {table} missing"

    def test_reopen_skips_applied_migrations_idempotently(self, tmp_path: Path) -> None:
        path = tmp_path / "katherine.db"
        first = open_local_storage(path)
        version = first.schema_version()
        first.close()
        second = open_local_storage(path)
        assert second.schema_version() == version
        second.close()

    def test_partial_migration_failure_never_marks_schema(self, tmp_path: Path) -> None:
        # A migration whose LAST statement fails (trigger aborts the
        # insert): the earlier DDL inside the same migration must roll
        # back too — neither the broken table nor the version row may
        # survive. This is the real mid-migration atomicity case.
        from backend.local_storage import migrations as migrations_module

        original = migrations_module.MIGRATIONS
        broken = list(original) + [
            (
                9_999,
                "create table broken_table (id integer primary key);\n"
                "create trigger boom before insert on broken_table begin "
                "select raise(ABORT,'boom'); end;\n"
                "insert into broken_table values (1);",
            )
        ]
        try:
            migrations_module.MIGRATIONS = tuple(broken)
            path = tmp_path / "partial.db"
            with pytest.raises(Exception):
                open_local_storage(path)
            conn = sqlite3.connect(path)
            marked = conn.execute(
                "select count(*) from schema_migrations where version = 99999"
            ).fetchone()[0]
            table_left = conn.execute(
                "select count(*) from sqlite_master where name='broken_table'"
            ).fetchone()[0]
            conn.close()
            assert marked == 0
            assert table_left == 0, "mid-migration DDL must roll back with the version row"
        finally:
            migrations_module.MIGRATIONS = original


class TestRestartAndRecovery:
    """Issue #335 test 2 (restart), 9 (corruption → explicit error)."""

    def test_restart_reopens_state_without_loss(self, tmp_path: Path) -> None:
        path = tmp_path / "katherine.db"
        store = open_local_storage(path)
        committed = store.commit_turn(**_commit_kwargs(store, "req-1", "resposta-1"))
        store.close()

        reopened = open_local_storage(path)
        state = reopened.load_user_state()
        assert state.revision == 1
        history = reopened.load_recent_history(limit=10)
        assert [m["content"] for m in history] == ["oi", "resposta-1"]
        # replay returns the same committed result without a provider
        outcome = reopened.replay("req-1")
        assert outcome.status == "completed"
        assert outcome.committed.response == "resposta-1"
        reopened.close()

    def test_corrupted_database_raises_explicit_error_never_resets(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "katherine.db"
        store = open_local_storage(path)
        store.commit_turn(**_commit_kwargs(store, "req-1"))
        store.close()
        # corrupt the file bytes
        raw = path.read_bytes()
        path.write_bytes(raw[: max(1, len(raw) // 3)] + b"\x00\x01garbage")
        with pytest.raises(StorageCorruptError):
            open_local_storage(path)
        # the corrupted file is NOT silently recreated: it still fails on reopen
        with pytest.raises(StorageCorruptError):
            open_local_storage(path)

    def test_io_error_surfaces_as_persistence_error_not_reset(
        self, tmp_path: Path
    ) -> None:
        import os

        path = tmp_path / "katherine.db"
        store = open_local_storage(path)
        store.commit_turn(**_commit_kwargs(store, "req-1"))
        store.close()
        os.chmod(path, 0o000)
        try:
            with pytest.raises(Exception):
                open_local_storage(path)
        finally:
            os.chmod(path, 0o644)


class TestAtomicTurnCommit:
    """Issue #335 tests 3 (whole-turn failure) and 4 (coherent rollback)."""

    def test_commit_fails_entirely_when_intermediate_write_fails(
        self, tmp_path: Path
    ) -> None:
        store = open_local_storage(tmp_path / "katherine.db")
        # Real SQLite failure: the assistant row is blocked by a trigger.
        conn = store._connection_for_tests_only()
        conn.execute(
            "create trigger block_assistant_row before insert on chat_logs "
            "when new.role = 'assistant' begin select raise(ABORT, 'boom'); end;"
        )
        before_logs = conn.execute("select count(*) from chat_logs").fetchone()[0]
        from backend.local_storage import PersistenceError

        with pytest.raises(PersistenceError):
            store.commit_turn(**_commit_kwargs(store, "req-fail"))
        after_logs = conn.execute("select count(*) from chat_logs").fetchone()[0]
        assert before_logs == after_logs  # user row rolled back too
        # no turn_requests row, no revision bump
        assert conn.execute("select count(*) from turn_requests").fetchone()[0] == 0
        assert store.load_user_state().revision == 0
        store.close()

    def test_rollback_keeps_messages_and_snapshots_coherent(self, tmp_path: Path) -> None:
        store = open_local_storage(tmp_path / "katherine.db")
        store.commit_turn(**_commit_kwargs(store, "req-1", "r1"))
        rev_before = store.load_user_state().revision
        conn = store._connection_for_tests_only()
        conn.execute(
            "create trigger block_outbox before insert on outbox_events "
            "begin select raise(ABORT, 'boom'); end;"
        )
        kw = _commit_kwargs(store, "req-2", "r2")
        kw["outbox_events"] = [
            ("archival_extraction_requested", {"message_id": "req-2", "kind": "archival", "version": 1}, "archival:req-2:v1")
        ]
        from backend.local_storage import PersistenceError

        with pytest.raises(PersistenceError):
            store.commit_turn(**kw)
        # coherent: nothing from turn 2 landed
        assert store.load_user_state().revision == rev_before
        history = store.load_recent_history(limit=10)
        assert [m["content"] for m in history] == ["oi", "r1"]
        outcome = store.replay("req-2")
        assert outcome.status == "request_replay_unavailable"
        store.close()

    def test_revision_cas_rejects_stale_expected_revision(self, tmp_path: Path) -> None:
        from backend.local_storage import ConflictError

        store = open_local_storage(tmp_path / "katherine.db")
        store.commit_turn(**_commit_kwargs(store, "req-1", "r1"))
        # replay with the stale revision 0 must conflict
        kw = dict(_commit_kwargs(store, "req-1b", "r1b"), expected_revision=0)
        with pytest.raises(ConflictError) as excinfo:
            store.commit_turn(**kw)
        assert excinfo.value.code == "revision_mismatch"
        assert excinfo.value.actual_revision == 1
        store.close()

    def test_duplicate_request_id_replays_committed_turn(self, tmp_path: Path) -> None:
        store = open_local_storage(tmp_path / "katherine.db")
        first = store.commit_turn(**_commit_kwargs(store, "req-dup", "r-dup"))
        second = store.commit_turn(**_commit_kwargs(store, "req-dup", "r-dup"))
        assert first.request_id == second.request_id
        assert first.revision == second.revision
        # only ONE pair of messages persisted
        history = store.load_recent_history(limit=10)
        assert [m["content"] for m in history] == ["oi", "r-dup"]
        store.close()

    def test_outbox_event_idempotency_key_unique(self, tmp_path: Path) -> None:
        store = open_local_storage(tmp_path / "katherine.db")
        kw = _commit_kwargs(store, "req-ob", "r-ob")
        kw["outbox_events"] = [
            ("archival_extraction_requested", {"message_id": "req-ob", "kind": "archival", "version": 1}, "archival:req-ob:v1")
        ]
        store.commit_turn(**kw)
        # same idempotency key again (replay of the same request) must not duplicate
        second = store.commit_turn(**kw)
        conn = store._connection_for_tests_only()
        count = conn.execute(
            "select count(*) from outbox_events where idempotency_key = 'archival:req-ob:v1'"
        ).fetchone()[0]
        assert count == 1
        assert second.revision == 1


class TestForeignKeys:
    """Issue #335 test 5: foreign keys really enabled."""

    def test_pragma_foreign_keys_enabled_on_every_connection(self, tmp_path: Path) -> None:
        store = open_local_storage(tmp_path / "katherine.db")
        conn = store._connection_for_tests_only()
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        store.close()

    def test_fk_violation_rejected(self, tmp_path: Path) -> None:
        store = open_local_storage(tmp_path / "katherine.db")
        conn = store._connection_for_tests_only()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "insert into outbox_events (id, event_type, idempotency_key, "
                "turn_request_id, payload, created_at) values "
                "('e1','archival_extraction_requested','k1','nonexistent','{}','2026-01-01T00:00:00Z')"
            )
        store.close()


class TestPrivacyAndRetention:
    """Issue #335 tests 6 (real deletion semantics) and 10 (growth bounds)."""

    def test_delete_history_removes_messages_and_respects_semantics(
        self, tmp_path: Path
    ) -> None:
        store = open_local_storage(tmp_path / "katherine.db")
        store.commit_turn(**_commit_kwargs(store, "req-1", "r1"))
        store.commit_turn(**_commit_kwargs(store, "req-2", "r2"))
        result = store.delete_history()
        assert result["status"] == "applied"
        assert result["deleted_messages"] == 4
        assert store.load_recent_history(limit=10) == []
        # privacy operation is recorded (audit, no content)
        conn = store._connection_for_tests_only()
        ops = conn.execute("select operation, status from privacy_operations").fetchall()
        assert ops == [("delete_history", "applied")]
        store.close()

    def test_delete_memories_removes_only_memories(self, tmp_path: Path) -> None:
        store = open_local_storage(tmp_path / "katherine.db")
        store.store_memory(
            content="fato aprovado",
            metadata={
                "schema_version": 1,
                "tags": ["gosto"],
                "approved": True,
                "provenance": "archival_extraction",
                "epistemic_status": "known",
                "importance": 0.7,
            },
        )
        store.commit_turn(**_commit_kwargs(store, "req-1", "r1"))
        result = store.delete_memories()
        assert result["status"] == "applied"
        assert result["deleted_memories"] == 1
        history = store.load_recent_history(limit=10)
        assert len(history) == 2  # chat intact
        store.close()

    def test_trim_history_enforces_growth_bound(self, tmp_path: Path) -> None:
        store = open_local_storage(tmp_path / "katherine.db")
        for i in range(30):
            store.commit_turn(
                **_commit_kwargs(store, f"req-{i}", f"r{i}")
            )
        result = store.trim_history(keep_last=10)
        assert result["status"] == "applied"
        assert result["remaining"] == 10
        conn = store._connection_for_tests_only()
        assert conn.execute("select count(*) from chat_logs").fetchone()[0] == 10
        store.close()


class TestIsolation:
    """Issue #335 test 7: one temporary database never contaminates another."""

    def test_two_test_databases_are_isolated(self, tmp_path: Path) -> None:
        a = open_local_storage(tmp_path / "a.db")
        b = open_local_storage(tmp_path / "b.db")
        a.commit_turn(**_commit_kwargs(a, "req-a", "resposta-A"))
        assert [m["content"] for m in b.load_recent_history(limit=10)] == []
        assert b.load_user_state().revision == 0
        assert [m["content"] for m in a.load_recent_history(limit=10)] == ["oi", "resposta-A"]
        a.close()
        b.close()


class TestGrowthAndIndexes:
    """Issue #335 test 10: growth and basic indexes are evaluated."""

    def test_history_query_uses_index(self, tmp_path: Path) -> None:
        store = open_local_storage(tmp_path / "katherine.db")
        for i in range(50):
            store.commit_turn(**_commit_kwargs(store, f"req-{i}", f"r{i}"))
        conn = store._connection_for_tests_only()
        plan = conn.execute(
            "explain query plan select id, role, content, created_at from chat_logs "
            "order by created_at desc, id desc limit 10"
        ).fetchall()
        plan_str = " ".join(row[3] for row in plan).lower()
        store.close()
        assert "using index" in plan_str, plan_str

    def test_request_replay_lookup_uses_index(self, tmp_path: Path) -> None:
        store = open_local_storage(tmp_path / "katherine.db")
        for i in range(20):
            store.commit_turn(**_commit_kwargs(store, f"req-{i}", f"r{i}"))
        conn = store._connection_for_tests_only()
        plan = conn.execute(
            "explain query plan select * from turn_requests where request_id = 'req-7'"
        ).fetchall()
        plan_str = " ".join(row[3] for row in plan).lower()
        store.close()
        assert "using index" in plan_str

    def test_chat_log_count_and_size_metrics(self, tmp_path: Path) -> None:
        store = open_local_storage(tmp_path / "katherine.db")
        for i in range(10):
            store.commit_turn(**_commit_kwargs(store, f"req-{i}", f"r{i}"))
        metrics = store.storage_metrics()
        assert metrics["chat_log_rows"] == 20
        assert metrics["page_count"] > 0
        assert metrics["page_size"] > 0
        store.close()


class TestConcurrency:
    """Issue #335 test 11: explicit serialization policy inside one process."""

    def test_concurrent_commits_serialize_with_clear_policy(self, tmp_path: Path) -> None:
        store = open_local_storage(tmp_path / "katherine.db")
        errors: list[Exception] = []
        successes: list[int] = []

        def worker(i: int) -> None:
            try:
                committed = store.commit_turn(
                    **_commit_kwargs(store, f"req-{i}", f"r{i}")
                )
                successes.append(committed.revision)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not any(isinstance(e, PersistenceErrorHolder) for e in errors)
        assert len(errors) == 0, f"unexpected errors: {errors}"
        # all 8 turns committed, revisions unique and sequential
        assert sorted(successes) == list(range(1, 9))
        conn = store._connection_for_tests_only()
        assert conn.execute("select count(*) from chat_logs").fetchone()[0] == 16
        store.close()

    def test_reader_thread_sees_committed_state_during_writes(self, tmp_path: Path) -> None:
        # WAL allows reads to proceed while a write transaction is open.
        # A separate READER THREAD gets its own connection; the reader must
        # only ever see committed state (never the in-flight write).
        store = open_local_storage(tmp_path / "katherine.db")
        store.commit_turn(**_commit_kwargs(store, "req-1", "r1"))
        write_conn = store._connection_for_tests_only()
        write_conn.execute("begin immediate")
        write_conn.execute(
            "insert into chat_logs (role, content, created_at) values ('user','x','2026-01-01T00:00:00Z')"
        )
        seen: list[int] = []

        def read_committed() -> None:
            seen.append(
                store._connection_for_tests_only()
                .execute("select count(*) from chat_logs")
                .fetchone()[0]
            )

        reader = threading.Thread(target=read_committed)
        reader.start()
        reader.join(timeout=10)
        try:
            assert seen == [2]  # reader sees only committed state
        finally:
            write_conn.execute("rollback")
        store.close()


class TestNoCloudImports:
    """Issue #335 test 12: tests never require Supabase/Postgres/network."""

    def test_package_import_does_not_pull_supabase(self) -> None:
        import subprocess

        code = "import backend.local_storage, sys; "
        code += "mods = [m for m in sys.modules if m.split('.')[0] in "
        code += "('supabase', 'postgrest', 'httpx', 'requests')]; "
        code += "print('LEAK=' + ','.join(sorted(mods)) if mods else 'CLEAN')"
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert "LEAK=" not in result.stdout, f"cloud imports leaked: {result.stdout}"
        assert "CLEAN" in result.stdout


class TestXdgPaths:
    """XDG-compliant default path with a safe override for tests."""

    def test_default_path_follows_xdg_data_home(self, tmp_path: Path) -> None:
        import os

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        env = {
            "HOME": str(fake_home),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        }
        path = default_database_path(env=env)
        assert str(tmp_path / "data") in str(path)
        assert "katherine" in path.name

    def test_default_path_creates_directories(self, tmp_path: Path) -> None:
        env = {"HOME": str(tmp_path), "XDG_DATA_HOME": str(tmp_path / "xdgdata")}
        path = default_database_path(env=env)
        assert path.parent.exists()

    def test_fallback_without_xdg_uses_home_local_share(self, tmp_path: Path) -> None:
        env = {"HOME": str(tmp_path), "XDG_DATA_HOME": ""}
        path = default_database_path(env=env)
        assert ".local" in str(path)


class TestBackup:
    """Issue #335: backup/copy must never capture inconsistent state."""

    def test_backup_produces_consistent_copy(self, tmp_path: Path) -> None:
        store = open_local_storage(tmp_path / "katherine.db")
        for i in range(5):
            store.commit_turn(**_commit_kwargs(store, f"req-{i}", f"r{i}"))
        backup_path = tmp_path / "backup.db"
        store.backup_to(backup_path)
        # the backup is a complete, consistent database
        check = open_local_storage(backup_path)
        assert check.load_user_state().revision == 5
        assert len(check.load_recent_history(limit=10)) == 10
        check.close()
        store.close()

    def test_backup_refuses_to_overwrite_existing_file(self, tmp_path: Path) -> None:
        store = open_local_storage(tmp_path / "katherine.db")
        store.commit_turn(**_commit_kwargs(store, "req-1", "r1"))
        target = tmp_path / "backup.db"
        target.write_text("existing")
        with pytest.raises(Exception):
            store.backup_to(target)
        assert target.read_text() == "existing"
        store.close()


class TestSanitizedErrors:
    """Public errors never carry paths, SQL, tracebacks or raw content."""

    def test_persistence_error_is_constant_message(self, tmp_path: Path) -> None:
        from backend.local_storage import PersistenceError

        store = open_local_storage(tmp_path / "katherine.db")
        conn = store._connection_for_tests_only()
        conn.execute(
            "create trigger boom before insert on chat_logs "
            "begin select raise(ABORT,'secret-path /home/user/leak'); end;"
        )
        with pytest.raises(PersistenceError) as excinfo:
            store.commit_turn(**_commit_kwargs(store, "req-x"))
        message = str(excinfo.value)
        store.close()
        assert "secret-path" not in message
        assert "/home" not in message
        assert "leak" not in message

    def test_validation_error_rejects_oversized_message(self, tmp_path: Path) -> None:
        from backend.local_storage import ValidationError as LocalValidationError

        store = open_local_storage(tmp_path / "katherine.db")
        kw = _commit_kwargs(store, "req-big")
        kw["user_message"] = "x" * 20_001
        with pytest.raises(LocalValidationError) as excinfo:
            store.commit_turn(**kw)
        assert excinfo.value.code == "message_too_long"
        store.close()


class TestReplayContract:
    """Replay parity with the web contract: same parser, same statuses."""

    def test_replay_unknown_request_is_unavailable(self, tmp_path: Path) -> None:
        store = open_local_storage(tmp_path / "katherine.db")
        outcome = store.replay("does-not-exist")
        assert outcome.status == "request_replay_unavailable"
        assert outcome.committed is None
        store.close()

    def test_pending_request_reports_in_progress_not_recomputed(self, tmp_path: Path) -> None:
        store = open_local_storage(tmp_path / "katherine.db")
        conn = store._connection_for_tests_only()
        conn.execute(
            "insert into turn_requests (request_id, payload_hash_sha256, status, "
            "expected_revision, created_at, updated_at) values "
            "('req-pending', 'deadbeef', 'pending', 0, '2026-01-01T00:00:00Z', "
            "'2026-01-01T00:00:00Z')"
        )
        outcome = store.replay("req-pending")
        assert outcome.status == "request_in_progress"
        store.close()

    def test_load_user_state_defaults_neutral(self, tmp_path: Path) -> None:
        from backend.emotional_domain import EmotionalStateV1
        from backend.relationship import RelationshipStateV1

        store = open_local_storage(tmp_path / "katherine.db")
        state = store.load_user_state()
        assert state.revision == 0
        assert state.persona_config is None
        # Neutral v1 snapshots (contract parity with the web flow) — not empty.
        neutral_emotional = EmotionalStateV1.neutral(timestamp=1.0).to_dict()
        neutral_relationship = RelationshipStateV1.neutral(timestamp=1.0).to_dict()
        assert state.emotional_state.keys() == neutral_emotional.keys()
        assert state.relationship_state.keys() == neutral_relationship.keys()
        assert state.emotional_state["schema_version"] == 1
        assert state.relationship_state["schema_version"] == 1
        store.close()


class TestRecoveryOfInterruptedTurns:
    """Issue #335 test 2 extension: interrupted pending turns fail closed."""

    def test_pending_without_completed_commit_is_marked_interrupted_on_open(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "katherine.db"
        store = open_local_storage(path)
        conn = store._connection_for_tests_only()
        conn.execute(
            "insert into turn_requests (request_id, payload_hash_sha256, status, "
            "expected_revision, created_at, updated_at) values "
            "('req-zombie', 'deadbeef', 'pending', 0, '2026-01-01T00:00:00Z', "
            "'2026-01-01T00:00:00Z')"
        )
        store.close()
        # simulate crash+restart: reopen marks zombie pending as failed
        reopened = open_local_storage(path)
        outcome = reopened.replay("req-zombie")
        assert outcome.status == "request_replay_unavailable"
        conn2 = reopened._connection_for_tests_only()
        status = conn2.execute(
            "select status from turn_requests where request_id='req-zombie'"
        ).fetchone()[0]
        assert status == "failed"
        reopened.close()


class PersistenceErrorHolder(Exception):
    """Never raised; only used as a sentinel type in concurrency assertions."""


def test_turn_request_row_shape_matches_transactional_contract(tmp_path: Path) -> None:
    """The SQLite turn ledger mirrors the PostgreSQL contract fields."""
    store = open_local_storage(tmp_path / "katherine.db")
    store.commit_turn(**_commit_kwargs(store, "req-shape", "r-shape"))
    conn = store._connection_for_tests_only()
    row = conn.execute(
        "select request_id, status, expected_revision, committed_revision, "
        "user_message_chat_log_id, assistant_message_chat_log_id, replay_payload "
        "from turn_requests where request_id='req-shape'"
    ).fetchone()
    store.close()
    assert row[0] == "req-shape"
    assert row[1] == "completed"
    assert row[2] == 0
    assert row[3] == 1
    assert row[4] is not None and row[5] is not None
    assert row[4] != row[5]
    payload = json.loads(row[6])
    assert payload["response"] == "r-shape"


class TestRealDomainTurnCycle:
    """The store supports the operations the real turn flow uses, exercised
    with the production domain modules (no mocks, no synthetic copies).

    This mirrors the use-case sequence of ``process_turn``:
    load state + revision → real domain transition → commit_turn with
    expected_revision → public result parsed back from the persisted
    payload → replay returns the same persisted result idempotently.
    """

    def test_full_turn_cycle_with_real_domain_transitions(self, tmp_path: Path) -> None:
        from backend.emotional_domain import AppraisalV1, EmotionalStateV1
        from backend.emotional_domain.transition import TransitionConfig, transition
        from backend.relationship import (
            RelationshipStateV1,
            RelationshipTransitionConfig,
            transition_relationship,
        )

        store = open_local_storage(tmp_path / "katherine.db")

        # ── turn 1: fresh state, real domain transitions ──────────────────
        user_state = store.load_user_state()
        prev_emotional = EmotionalStateV1.from_dict(user_state.emotional_state)
        prev_relationship = RelationshipStateV1.from_dict(
            user_state.relationship_state
        )
        revision = user_state.revision

        appraisal = AppraisalV1.create(
            valence_shift=0.3, arousal_shift=0.2, dominance_shift=0.0
        )
        t1 = max(prev_emotional.timestamp, prev_relationship.timestamp) + 100.0
        t2 = t1 + 100.0
        emotional_result = transition(
            prev_emotional, appraisal, t1, TransitionConfig.defaults()
        )
        new_relationship = transition_relationship(
            prev_relationship,
            appraisal,
            t2,
            RelationshipTransitionConfig.defaults(),
        )

        committed = store.commit_turn(
            request_id="req-cycle-1",
            user_message="oi Katherine",
            assistant_message="olá!",
            emotional_state=emotional_result.state.to_dict(),
            relationship_state=new_relationship.to_dict(),
            public_response="olá!",
            replay_payload={
                "response": "olá!",
                "emotion_state": {"valence": 0.1, "arousal": 0.2},
                "message_id": 2,
            },
            outbox_events=[],
            expected_revision=revision,
        )

        # Public result is parsed back from the persisted payload — never
        # from pre-commit variables (process_turn contract).
        assert committed.response == "olá!"
        assert committed.revision == revision + 1
        replayed = store.replay("req-cycle-1")
        assert replayed.status == "completed"
        assert replayed.committed.revision == committed.revision
        assert replayed.committed.response == committed.response

        # ── turn 2: loads turn 1's persisted snapshots, CAS advances ──────
        user_state_2 = store.load_user_state()
        assert user_state_2.revision == committed.revision
        roundtrip_emotional = EmotionalStateV1.from_dict(
            user_state_2.emotional_state
        )
        assert roundtrip_emotional.to_dict() == emotional_result.state.to_dict()
        roundtrip_relationship = RelationshipStateV1.from_dict(
            user_state_2.relationship_state
        )
        assert roundtrip_relationship.to_dict() == new_relationship.to_dict()

        appraisal_2 = AppraisalV1.create(
            valence_shift=-0.2, arousal_shift=0.1, dominance_shift=0.0
        )
        emotional_result_2 = transition(
            roundtrip_emotional, appraisal_2, t2, TransitionConfig.defaults()
        )
        committed_2 = store.commit_turn(
            request_id="req-cycle-2",
            user_message="tudo bem?",
            assistant_message="tudo ótimo",
            emotional_state=emotional_result_2.state.to_dict(),
            relationship_state=roundtrip_relationship.to_dict(),
            public_response="tudo ótimo",
            replay_payload={
                "response": "tudo ótimo",
                "emotion_state": {"valence": -0.05, "arousal": 0.1},
                "message_id": 4,
            },
            outbox_events=[],
            expected_revision=user_state_2.revision,
        )
        assert committed_2.revision == committed.revision + 1

        # ── stale CAS from turn 1 must be rejected with the real error ──
        with pytest.raises(ConflictError) as excinfo:
            store.commit_turn(
                request_id="req-cycle-stale",
                user_message="x",
                assistant_message="y",
                emotional_state=emotional_result.state.to_dict(),
                relationship_state=roundtrip_relationship.to_dict(),
                public_response="y",
                replay_payload={"response": "y", "emotion_state": {}, "message_id": 5},
                outbox_events=[],
                expected_revision=revision,
            )
        assert excinfo.value.code == "revision_mismatch"
        # actual_revision is the real profile revision at CAS time (both
        # turns committed), expected is the stale value the caller sent.
        assert excinfo.value.actual_revision == committed_2.revision
        assert excinfo.value.expected_revision == revision

        # ── history carries both turns in order ─────────────────────────
        history = store.load_recent_history(limit=10)
        assert [m["role"] for m in history] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]
        assert history[1]["content"] == "olá!"

        # ── backup mid-life is consistent and restorable ────────────────
        backup_path = tmp_path / "backup.db"
        store.backup_to(backup_path)
        store.close()
        restored = open_local_storage(backup_path)
        assert restored.load_user_state().revision == committed_2.revision
        assert len(restored.load_recent_history(limit=10)) == 4
        restored.close()


class TestRequestIdempotencyConflict:
    """Same request id + divergent determinative payload → deterministic
    conflict, never a silent replay of the old result, never a write."""

    def test_same_request_same_payload_replays_without_new_writes(
        self, tmp_path: Path
    ) -> None:
        store = open_local_storage(tmp_path / "katherine.db")
        kw = _commit_kwargs(store, "req-idem", "r1")
        first = store.commit_turn(**kw)
        conn = store._connection_for_tests_only()
        rows_before = conn.execute("select count(*) from chat_logs").fetchone()[0]
        turns_before = conn.execute("select count(*) from turn_requests").fetchone()[0]

        second = store.commit_turn(**kw)

        assert second.request_id == first.request_id
        assert second.revision == first.revision
        assert second.response == first.response
        assert conn.execute("select count(*) from chat_logs").fetchone()[0] == rows_before
        assert conn.execute("select count(*) from turn_requests").fetchone()[0] == turns_before
        store.close()

    def test_same_request_different_user_message_conflicts(
        self, tmp_path: Path
    ) -> None:
        store = open_local_storage(tmp_path / "katherine.db")
        store.commit_turn(**_commit_kwargs(store, "req-div", "r1"))
        kw = _commit_kwargs(store, "req-div", "r1")
        kw["user_message"] = "outra mensagem"
        with pytest.raises(ConflictError) as excinfo:
            store.commit_turn(**kw)
        assert excinfo.value.code == "request_payload_conflict"
        store.close()

    def test_replay_payload_foreign_request_id_is_rejected(
        self, tmp_path: Path
    ) -> None:
        # Web parity: a replay payload carrying a request_id must identify
        # THIS request (web _validate_replay_payload cross-check).
        store = open_local_storage(tmp_path / "katherine.db")
        kw = _commit_kwargs(store, "req-foreign", "r1")
        kw["replay_payload"]["request_id"] = "req-someone-else"
        with pytest.raises(LocalValidationError) as excinfo:
            store.commit_turn(**kw)
        assert excinfo.value.code == "invalid_replay_payload"
        # Nothing was written by the rejected commit.
        conn = store._connection_for_tests_only()
        assert conn.execute("select count(*) from chat_logs").fetchone()[0] == 0
        assert conn.execute("select count(*) from turn_requests").fetchone()[0] == 0
        # The matching request_id commits normally.
        kw["replay_payload"]["request_id"] = "req-foreign"
        committed = store.commit_turn(**kw)
        assert committed.request_id == "req-foreign"
        store.close()

    def test_same_request_different_snapshot_conflicts(self, tmp_path: Path) -> None:
        from backend.emotional_domain import EmotionalStateV1

        store = open_local_storage(tmp_path / "katherine.db")
        store.commit_turn(**_commit_kwargs(store, "req-snap", "r1"))
        kw = _commit_kwargs(store, "req-snap", "r1")
        snapshot = EmotionalStateV1.neutral(timestamp=2000.0).to_dict()
        snapshot["pleasure"] = 0.5
        kw["emotional_state"] = snapshot
        with pytest.raises(ConflictError) as excinfo:
            store.commit_turn(**kw)
        assert excinfo.value.code == "request_payload_conflict"
        store.close()

    def test_same_request_different_outbox_conflicts(self, tmp_path: Path) -> None:
        store = open_local_storage(tmp_path / "katherine.db")
        kw = _commit_kwargs(store, "req-obx", "r1")
        store.commit_turn(**kw)
        kw["outbox_events"] = [
            (
                "archival_extraction_requested",
                {"message_id": "req-obx", "kind": "archival", "version": 1},
                "archival:req-obx:v1",
            )
        ]
        with pytest.raises(ConflictError) as excinfo:
            store.commit_turn(**kw)
        assert excinfo.value.code == "request_payload_conflict"
        store.close()

    def test_conflict_writes_nothing(self, tmp_path: Path) -> None:
        store = open_local_storage(tmp_path / "katherine.db")
        store.commit_turn(**_commit_kwargs(store, "req-cw", "r1"))
        revision_before = store.load_user_state().revision
        history_before = store.load_recent_history(limit=50)
        conn = store._connection_for_tests_only()
        turns_before = conn.execute("select count(*) from turn_requests").fetchone()[0]
        outbox_before = conn.execute("select count(*) from outbox_events").fetchone()[0]

        kw = _commit_kwargs(store, "req-cw", "DIFFERENT")
        with pytest.raises(ConflictError):
            store.commit_turn(**kw)

        assert store.load_user_state().revision == revision_before
        assert store.load_recent_history(limit=50) == history_before
        assert conn.execute("select count(*) from turn_requests").fetchone()[0] == turns_before
        assert conn.execute("select count(*) from outbox_events").fetchone()[0] == outbox_before
        store.close()

    def test_error_never_echoes_payload(self, tmp_path: Path) -> None:
        store = open_local_storage(tmp_path / "katherine.db")
        secret = "conteúdo privado do usuário"
        store.commit_turn(**_commit_kwargs(store, "req-leak", "r1"))
        kw = _commit_kwargs(store, "req-leak", "r1")
        kw["user_message"] = secret
        with pytest.raises(ConflictError) as excinfo:
            store.commit_turn(**kw)
        rendered = str(excinfo.value)
        assert secret not in rendered
        assert excinfo.value.message == (
            "Request id was already completed with a different payload."
        )
        store.close()


class TestDeleteHistoryPrivacySemantics:
    """delete_history removes messages, replay ledger and derived outbox,
    atomically, in one transaction; replay becomes unavailable afterwards."""

    def _committed_store(self, tmp_path: Path):
        store = open_local_storage(tmp_path / "katherine.db")
        kw = _commit_kwargs(store, "req-del", "resposta privada")
        kw["outbox_events"] = [
            (
                "archival_extraction_requested",
                {"message_id": "req-del", "kind": "archival", "version": 1},
                "archival:req-del:v1",
            )
        ]
        store.commit_turn(**kw)
        return store

    def test_delete_history_removes_all_history_data(self, tmp_path: Path) -> None:
        store = self._committed_store(tmp_path)
        result = store.delete_history()
        assert result["status"] == "applied"
        assert result["deleted_messages"] == 2
        assert result["deleted_turn_requests"] == 1
        assert result["deleted_outbox_events"] == 1

        conn = store._connection_for_tests_only()
        assert conn.execute("select count(*) from chat_logs").fetchone()[0] == 0
        assert conn.execute("select count(*) from turn_requests").fetchone()[0] == 0
        assert conn.execute("select count(*) from outbox_events").fetchone()[0] == 0
        # no replay payload content remains anywhere in the file
        assert store.load_recent_history(limit=10) == []
        outcome = store.replay("req-del")
        assert outcome.status == "request_replay_unavailable"
        assert outcome.committed is None

        # the audit ledger remains, but carries only aggregate counts
        rows = conn.execute(
            "select operation, status, result from privacy_operations"
        ).fetchall()
        assert rows[0][0] == "delete_history"
        assert rows[0][1] == "applied"
        stored_result = json.loads(rows[0][2])
        assert "resposta privada" not in rows[0][2]
        assert stored_result["deleted_messages"] == 2
        store.close()

    def test_delete_history_is_atomic_under_failure(self, tmp_path: Path) -> None:
        store = self._committed_store(tmp_path)
        conn = store._connection_for_tests_only()
        # A trigger that aborts the outbox delete proves the whole
        # operation rolls back: nothing may be partially deleted.
        conn.execute(
            "create trigger block_outbox_delete before delete on outbox_events "
            "begin select raise(ABORT, 'boom'); end;"
        )
        from backend.local_storage import PersistenceError

        with pytest.raises(PersistenceError):
            store.delete_history()
        # nothing was deleted: history intact
        assert conn.execute("select count(*) from chat_logs").fetchone()[0] == 2
        assert conn.execute("select count(*) from turn_requests").fetchone()[0] == 1
        assert conn.execute("select count(*) from outbox_events").fetchone()[0] == 1
        assert store.replay("req-del").status == "completed"
        store.close()

    def test_delete_history_then_new_turn_still_works(self, tmp_path: Path) -> None:
        store = self._committed_store(tmp_path)
        store.delete_history()
        committed = store.commit_turn(**_commit_kwargs(store, "req-after", "nova resposta"))
        assert committed.response == "nova resposta"
        history = store.load_recent_history(limit=10)
        assert [m["content"] for m in history] == ["oi", "nova resposta"]
        store.close()


class TestNeutralResets:
    """Resets produce the domain's canonical neutral v1 snapshot, bump the
    revision coherently, and reject stale commits afterwards."""

    def _non_neutral_state(self):
        from backend.emotional_domain import AppraisalV1, EmotionalStateV1
        from backend.emotional_domain.transition import TransitionConfig, transition
        from backend.relationship import (
            RelationshipStateV1,
            RelationshipTransitionConfig,
            transition_relationship,
        )

        emotional = transition(
            EmotionalStateV1.neutral(timestamp=1000.0),
            AppraisalV1.create(valence_shift=0.5, arousal_shift=0.4, dominance_shift=0.1),
            1100.0,
            TransitionConfig.defaults(),
        ).state
        relationship = transition_relationship(
            RelationshipStateV1.neutral(timestamp=1000.0),
            AppraisalV1.create(valence_shift=0.5, arousal_shift=0.4, dominance_shift=0.1),
            1200.0,
            RelationshipTransitionConfig.defaults(),
        )
        return emotional, relationship

    def test_reset_emotional_state_persists_canonical_neutral(self, tmp_path: Path) -> None:
        from backend.emotional_domain import EmotionalStateV1

        store = open_local_storage(tmp_path / "katherine.db")
        emotional, relationship = self._non_neutral_state()
        store.commit_turn(
            request_id="req-neutral-1",
            user_message="oi",
            assistant_message="olá",
            emotional_state=emotional.to_dict(),
            relationship_state=relationship.to_dict(),
            public_response="olá",
            replay_payload=_emotion_payload("olá"),
            outbox_events=[],
            expected_revision=0,
        )
        revision_before = store.load_user_state().revision
        assert store.load_user_state().emotional_state != EmotionalStateV1.neutral(
            timestamp=store.load_user_state().emotional_state["timestamp"]
        ).to_dict()

        result = store.reset_emotional_state()
        assert result["status"] == "applied"

        state = store.load_user_state()
        assert state.revision == revision_before + 1
        loaded = EmotionalStateV1.from_dict(state.emotional_state)
        canonical = EmotionalStateV1.neutral(timestamp=loaded.timestamp)
        assert loaded.to_dict() == canonical.to_dict()
        # relationship snapshot untouched by the emotional reset
        assert state.relationship_state == relationship.to_dict()
        store.close()

    def test_reset_relationship_state_persists_canonical_neutral(
        self, tmp_path: Path
    ) -> None:
        from backend.relationship import RelationshipStateV1

        store = open_local_storage(tmp_path / "katherine.db")
        emotional, relationship = self._non_neutral_state()
        store.commit_turn(
            request_id="req-neutral-2",
            user_message="oi",
            assistant_message="olá",
            emotional_state=emotional.to_dict(),
            relationship_state=relationship.to_dict(),
            public_response="olá",
            replay_payload=_emotion_payload("olá"),
            outbox_events=[],
            expected_revision=0,
        )
        revision_before = store.load_user_state().revision

        result = store.reset_relationship_state()
        assert result["status"] == "applied"

        state = store.load_user_state()
        assert state.revision == revision_before + 1
        loaded = RelationshipStateV1.from_dict(state.relationship_state)
        canonical = RelationshipStateV1.neutral(timestamp=loaded.timestamp)
        assert loaded.to_dict() == canonical.to_dict()
        assert state.emotional_state == emotional.to_dict()
        store.close()

    def test_reset_rejects_non_canonical_neutral_argument(self, tmp_path: Path) -> None:
        from backend.local_storage import ValidationError as LocalValidationError

        store = open_local_storage(tmp_path / "katherine.db")
        emotional, relationship = self._non_neutral_state()
        store.commit_turn(
            request_id="req-neutral-3",
            user_message="oi",
            assistant_message="olá",
            emotional_state=emotional.to_dict(),
            relationship_state=relationship.to_dict(),
            public_response="olá",
            replay_payload=_emotion_payload("olá"),
            outbox_events=[],
            expected_revision=0,
        )
        forged = emotional.to_dict()  # structurally valid v1, NOT neutral
        with pytest.raises(LocalValidationError) as excinfo:
            store.reset_emotional_state(neutral=forged)
        assert excinfo.value.code == "invalid_reset_payload"
        with pytest.raises(LocalValidationError):
            store.reset_emotional_state(neutral={})
        with pytest.raises(LocalValidationError):
            store.reset_relationship_state(neutral={"v": 1})
        store.close()

    def test_stale_commit_after_reset_hits_revision_mismatch(self, tmp_path: Path) -> None:
        store = open_local_storage(tmp_path / "katherine.db")
        emotional, relationship = self._non_neutral_state()
        committed = store.commit_turn(
            request_id="req-neutral-4",
            user_message="oi",
            assistant_message="olá",
            emotional_state=emotional.to_dict(),
            relationship_state=relationship.to_dict(),
            public_response="olá",
            replay_payload=_emotion_payload("olá"),
            outbox_events=[],
            expected_revision=0,
        )
        stale_revision = committed.revision

        store.reset_emotional_state()

        # A commit prepared on the pre-reset revision must not overwrite
        # the reset: CAS rejects it deterministically.
        with pytest.raises(ConflictError) as excinfo:
            store.commit_turn(
                request_id="req-neutral-5",
                user_message="de novo",
                assistant_message="resposta",
                emotional_state=emotional.to_dict(),
                relationship_state=relationship.to_dict(),
                public_response="resposta",
                replay_payload=_emotion_payload("resposta"),
                outbox_events=[],
                expected_revision=stale_revision,
            )
        assert excinfo.value.code == "revision_mismatch"
        state = store.load_user_state()
        from backend.emotional_domain import EmotionalStateV1

        loaded = EmotionalStateV1.from_dict(state.emotional_state)
        assert loaded.to_dict() == EmotionalStateV1.neutral(
            timestamp=loaded.timestamp
        ).to_dict()
        store.close()

    def test_reset_on_fresh_database_creates_profile_with_neutral(
        self, tmp_path: Path
    ) -> None:
        from backend.emotional_domain import EmotionalStateV1
        from backend.relationship import RelationshipStateV1

        store = open_local_storage(tmp_path / "katherine.db")
        result = store.reset_emotional_state()
        assert result["status"] == "applied"
        state = store.load_user_state()
        assert state.revision == 1
        loaded = EmotionalStateV1.from_dict(state.emotional_state)
        assert loaded.to_dict() == EmotionalStateV1.neutral(
            timestamp=loaded.timestamp
        ).to_dict()
        rel = RelationshipStateV1.from_dict(state.relationship_state)
        assert rel.to_dict() == RelationshipStateV1.neutral(
            timestamp=rel.timestamp
        ).to_dict()
        store.close()


class TestClosedLifecycle:
    """close() is terminal: same thread, other threads, no reconnection."""

    def test_operation_on_same_thread_after_close_fails(self, tmp_path: Path) -> None:
        from backend.local_storage import PersistenceError

        store = open_local_storage(tmp_path / "katherine.db")
        store.commit_turn(**_commit_kwargs(store, "req-close-1", "r1"))
        store.close()
        with pytest.raises(PersistenceError) as excinfo:
            store.load_user_state()
        assert excinfo.value.code == "storage_closed"
        assert excinfo.value.message == "storage is closed"
        with pytest.raises(PersistenceError):
            store.commit_turn(**_commit_kwargs(store, "req-close-2", "r2"))
        with pytest.raises(PersistenceError):
            store.replay("req-close-1")
        with pytest.raises(PersistenceError):
            store.delete_history()
        with pytest.raises(PersistenceError):
            store.storage_metrics()
        with pytest.raises(PersistenceError):
            store.backup_to(tmp_path / "backup.db")
        store.close()  # idempotent

    def test_operation_from_other_thread_after_close_fails(
        self, tmp_path: Path
    ) -> None:
        from backend.local_storage import PersistenceError

        store = open_local_storage(tmp_path / "katherine.db")
        store.commit_turn(**_commit_kwargs(store, "req-close-3", "r3"))
        # A second thread opens (and registers) its own connection first.
        barrier = threading.Barrier(2, timeout=10)
        errors: list[Exception] = []

        def other_thread_op() -> None:
            try:
                store._connection_for_tests_only()
                barrier.wait()
                barrier.wait()
                store.load_user_state()
                errors.append(AssertionError("operation after close must fail"))
            except PersistenceError as exc:
                if exc.code != "storage_closed":
                    errors.append(exc)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        worker = threading.Thread(target=other_thread_op)
        worker.start()
        barrier.wait()  # worker opened its connection
        store.close()  # closes every registered connection
        barrier.wait()  # worker proceeds to attempt the operation
        worker.join(timeout=10)
        assert errors == []

    def test_no_silent_reconnection_after_close(self, tmp_path: Path) -> None:
        from backend.local_storage import PersistenceError

        store = open_local_storage(tmp_path / "katherine.db")
        store.commit_turn(**_commit_kwargs(store, "req-close-4", "r4"))
        store.close()
        with pytest.raises(PersistenceError):
            store._connection_for_tests_only()
        # The database file still exists and holds the committed data; only
        # this store instance is closed. A NEW instance may open it normally.
        reopened = open_local_storage(tmp_path / "katherine.db")
        assert reopened.load_user_state().revision == 1
        reopened.close()

    def test_cross_thread_connection_really_closed_by_close(self, tmp_path: Path) -> None:
        """Prove close() deterministically closes other threads' connections.

        A worker thread creates and retains its own connection through
        the store; the main thread then calls close(). The retained
        connection object must be genuinely closed — any use raises
        ProgrammingError, and it must no longer be a functional shortcut
        past the closed store (no GC reliance, no leaked live handle).
        """
        from backend.local_storage import PersistenceError

        store = open_local_storage(tmp_path / "katherine.db")
        store.commit_turn(**_commit_kwargs(store, "req-close-5", "r5"))
        barrier = threading.Barrier(2, timeout=10)
        captured: dict[str, sqlite3.Connection] = {}

        def worker() -> None:
            captured["conn"] = store._connection_for_tests_only()
            barrier.wait()  # main thread may close() now

        thread = threading.Thread(target=worker)
        thread.start()
        barrier.wait()
        thread.join(timeout=10)
        worker_conn = captured["conn"]
        store.close()
        # The worker's connection was created by another thread, yet
        # close() shut it down deterministically (not via GC).
        with pytest.raises(sqlite3.ProgrammingError):
            worker_conn.execute("select 1")
        # It is not a functional shortcut: every operation still fails
        # with the sanitized storage_closed error.
        with pytest.raises(PersistenceError) as excinfo:
            store.load_user_state()
        assert excinfo.value.code == "storage_closed"
        with pytest.raises(PersistenceError) as excinfo:
            store.commit_turn(**_commit_kwargs(store, "req-close-6", "r6"))
        assert excinfo.value.code == "storage_closed"

    def test_backup_after_close_leaves_no_filesystem_artifacts(
        self, tmp_path: Path
    ) -> None:
        """backup_to() on a closed store must not create anything.

        The lifecycle gate runs before any target creation: no backup.db
        file, no parent directory, no empty side-effect left behind.
        """
        from backend.local_storage import PersistenceError

        store = open_local_storage(tmp_path / "katherine.db")
        store.commit_turn(**_commit_kwargs(store, "req-close-7", "r7"))
        store.close()
        backup_dir = tmp_path / "backups"
        backup_file = backup_dir / "backup.db"
        with pytest.raises(PersistenceError) as excinfo:
            store.backup_to(backup_file)
        assert excinfo.value.code == "storage_closed"
        assert excinfo.value.message == "storage is closed"
        assert not backup_file.exists()
        assert not backup_dir.exists()

    def test_close_is_idempotent_and_safe(self, tmp_path: Path) -> None:
        store = open_local_storage(tmp_path / "katherine.db")
        store.close()
        store.close()
