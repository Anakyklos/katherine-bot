"""Regression tests for issue #376 P1: admission transaction release.

Real SQLite, temporary databases only. Proves that a failed
``reserve_request()`` (missing/corrupt ``history_fence``) releases the
write transaction and lock, and leaves no persisted rows behind.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.local_storage import PersistenceError, open_local_storage


def _corrupt_missing_row(db_path: Path, store) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("delete from history_fence")
        conn.commit()
    # keep reference to store's connection alive via tests-only accessor
    _ = store._connection_for_tests_only()


def test_missing_history_fence_row_releases_transaction(tmp_path: Path) -> None:
    db_path = tmp_path / "katherine.db"
    store = open_local_storage(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("delete from history_fence")
        conn.commit()

    conn = store._connection_for_tests_only()
    with pytest.raises(PersistenceError) as excinfo:
        store.reserve_request("req-missing-row", "oi")
    assert excinfo.value.code == "database_error"
    assert conn.in_transaction is False

    # Independent writer can acquire the write lock immediately.
    with sqlite3.connect(db_path, timeout=1) as independent:
        independent.execute("BEGIN IMMEDIATE")
        independent.execute("ROLLBACK")

    # No reservation/message/turn persisted.
    with sqlite3.connect(db_path) as check:
        assert check.execute("select count(*) from turn_requests").fetchone()[0] == 0
        assert check.execute("select count(*) from chat_logs").fetchone()[0] == 0

    # Subsequent operation does not see a nested transaction.
    store.delete_memories()
    store.close()


def test_missing_history_fence_table_releases_transaction(tmp_path: Path) -> None:
    db_path = tmp_path / "katherine.db"
    store = open_local_storage(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("drop table history_fence")
        conn.commit()

    conn = store._connection_for_tests_only()
    with pytest.raises(PersistenceError) as excinfo:
        store.reserve_request("req-missing-table", "oi")
    assert excinfo.value.code == "database_error"
    assert conn.in_transaction is False

    with sqlite3.connect(db_path, timeout=1) as independent:
        independent.execute("BEGIN IMMEDIATE")
        independent.execute("ROLLBACK")

    with sqlite3.connect(db_path) as check:
        assert check.execute("select count(*) from turn_requests").fetchone()[0] == 0
        assert check.execute("select count(*) from chat_logs").fetchone()[0] == 0

    store.delete_memories()
    store.close()
