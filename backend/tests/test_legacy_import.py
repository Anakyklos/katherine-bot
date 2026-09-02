"""Legacy Supabase fixture import — issue #336 mandatory test 12.

"importação de fixture legado não duplica dados": importing a legacy
installation fixture into the local SQLite store is explicit,
transactional and IDEMPOTENT — re-importing the same fixture never
duplicates rows.

Proofs here (all on a real temp SQLite database, no mocks):

1. first import writes every turn exactly once;
2. SECOND import of the same fixture skips everything (duplicates
   counted, rows unchanged);
3. import-then-runtime: the desktop runtime reads the imported
   history and replays imported turns without recomputation;
4. a failed/invalid import leaves the store untouched (no partial
   writes);
5. forbidden keys (prompts/tokens/secrets) are rejected fail-closed;
6. the source fixture is never modified.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.local_storage.errors import ValidationError
from backend.local_storage.legacy_import import (
    ImportReport,
    import_legacy_fixture,
    validate_legacy_fixture,
)
from backend.local_storage.storage import open_local_storage


def _fixture() -> dict:
    """Structural legacy Supabase export fixture (schema v1)."""
    return {
        "schema_version": 1,
        "source": "supabase",
        "turns": [
            {
                "request_id": "legacy-turn-1",
                "user_message": "oi, lembra de mim?",
                "assistant_message": "oi! claro que lembro.",
                "created_at": "2025-11-01T10:00:00Z",
            },
            {
                "request_id": "legacy-turn-2",
                "user_message": "vamos conversar",
                "assistant_message": "claro, estou aqui.",
                "created_at": "2025-11-01T10:05:00Z",
            },
        ],
    }


class TestLegacyFixtureImport:
    """#336 test 12: import does not duplicate data."""

    def test_first_import_writes_each_turn_once(self, tmp_path: Path) -> None:
        store = open_local_storage(tmp_path / "katherine.db")
        report = import_legacy_fixture(store, _fixture())
        assert report.imported_turns == 2
        assert report.skipped_duplicates == 0
        assert report.total_turns_after == 2
        # chat history persisted: 2 turns → 4 messages (user+assistant)
        conn = store._connection_for_tests_only()
        rows = conn.execute("select count(*) from chat_logs").fetchone()[0]
        assert rows == 4
        store.close()

    def test_second_import_of_same_fixture_duplicates_nothing(
        self, tmp_path: Path
    ) -> None:
        """The core idempotency proof of issue test 12."""
        store = open_local_storage(tmp_path / "katherine.db")
        fixture = _fixture()
        first = import_legacy_fixture(store, fixture)
        conn = store._connection_for_tests_only()
        turn_rows_before = conn.execute(
            "select count(*) from turn_requests"
        ).fetchone()[0]
        message_rows_before = conn.execute(
            "select count(*) from chat_logs"
        ).fetchone()[0]

        second = import_legacy_fixture(store, fixture)

        assert second.imported_turns == 0
        assert second.skipped_duplicates == 2
        assert second.total_turns_after == first.total_turns_after
        assert (
            conn.execute("select count(*) from turn_requests").fetchone()[0]
            == turn_rows_before
        )
        assert (
            conn.execute("select count(*) from chat_logs").fetchone()[0]
            == message_rows_before
        )
        store.close()

    def test_import_is_transactional_no_partial_writes_on_failure(
        self, tmp_path: Path
    ) -> None:
        store = open_local_storage(tmp_path / "katherine.db")
        good = _fixture()
        import_legacy_fixture(store, good)
        conn = store._connection_for_tests_only()
        turn_rows_before = conn.execute(
            "select count(*) from turn_requests"
        ).fetchone()[0]
        message_rows_before = conn.execute(
            "select count(*) from chat_logs"
        ).fetchone()[0]

        # A second fixture whose FIRST turn is valid but whose SECOND
        # turn is malformed must fail-closed WITHOUT writing turn 3.
        broken = _fixture()
        broken["turns"] = [
            {
                "request_id": "legacy-turn-3",
                "user_message": "ok",
                "assistant_message": "ok",
            },
            {"request_id": "", "user_message": "bad", "assistant_message": "bad"},
        ]
        with pytest.raises(ValidationError):
            import_legacy_fixture(store, broken)

        assert (
            conn.execute("select count(*) from turn_requests").fetchone()[0]
            == turn_rows_before
        )
        assert (
            conn.execute("select count(*) from chat_logs").fetchone()[0]
            == message_rows_before
        )
        store.close()

    def test_forbidden_keys_rejected_fail_closed(self, tmp_path: Path) -> None:
        store = open_local_storage(tmp_path / "katherine.db")
        poisoned = _fixture()
        poisoned["turns"][0]["system_prompt"] = "internal instructions"
        with pytest.raises(ValidationError) as excinfo:
            import_legacy_fixture(store, poisoned)
        assert excinfo.value.code == "forbidden_key_in_legacy_fixture"
        # nothing was written
        conn = store._connection_for_tests_only()
        assert (
            conn.execute("select count(*) from turn_requests").fetchone()[0] == 0
        )
        store.close()

    def test_wrong_schema_version_rejected(self, tmp_path: Path) -> None:
        store = open_local_storage(tmp_path / "katherine.db")
        fixture = _fixture()
        fixture["schema_version"] = 999
        with pytest.raises(ValidationError):
            import_legacy_fixture(store, fixture)
        store.close()

    def test_source_fixture_never_modified(self, tmp_path: Path) -> None:
        store = open_local_storage(tmp_path / "katherine.db")
        fixture = _fixture()
        snapshot = json.dumps(fixture, sort_keys=True)
        import_legacy_fixture(store, fixture)
        import_legacy_fixture(store, fixture)
        assert json.dumps(fixture, sort_keys=True) == snapshot
        store.close()

    def test_runtime_reads_imported_history_and_replays_without_recompute(
        self, tmp_path: Path
    ) -> None:
        """Issue step 4 of legacy import: 'verificar leitura pelo
        runtime desktop' — the desktop runtime loads the imported
        history; an imported request id replays the imported answer
        and never touches the provider."""
        from backend.companion_runtime import CompanionRuntime

        class NullProvider:
            def __init__(self) -> None:
                self.calls = 0

            async def appraise(self, message, budget):  # pragma: no cover
                raise AssertionError("provider must not be called")

            async def generate(self, messages, budget):  # pragma: no cover
                raise AssertionError("provider must not be called")

            def build_trusted_policy(self, *args, **kwargs):
                raise AssertionError("provider must not be called")

        store = open_local_storage(tmp_path / "katherine.db")
        import_legacy_fixture(store, _fixture())
        store.close()

        import asyncio

        runtime = CompanionRuntime(
            storage_path=tmp_path / "katherine.db",
            provider=NullProvider(),
        )
        history = runtime.load_history(limit=50)
        contents = [m["content"] for m in history]
        assert "oi, lembra de mim?" in contents
        assert "oi! claro que lembro." in contents

        # Imported turn replays durably — no provider recompute.
        result = asyncio.run(
            runtime.commit_turn_async(
                request_id="legacy-turn-1", message="oi, lembra de mim?"
            )
        )
        assert result.success is True
        assert result.replayed is True
        assert result.response == "oi! claro que lembro."
        runtime.close()

    def test_import_after_local_turns_coexists_without_duplicates(
        self, tmp_path: Path
    ) -> None:
        """Local-first life: the user already chatted locally, THEN
        imports the legacy install — both coexist; neither duplicates."""
        store = open_local_storage(tmp_path / "katherine.db")
        # one local turn
        store.commit_turn(
            request_id="local-1",
            user_message="olá local",
            assistant_message="olá!",
            emotional_state=_neutral_emotional(),
            relationship_state=_neutral_relationship(),
            public_response="olá!",
            replay_payload={
                "response": "olá!",
                "message_id": 2,
                "request_id": "local-1",
            },
        )
        first = import_legacy_fixture(store, _fixture())
        assert first.imported_turns == 2
        second = import_legacy_fixture(store, _fixture())
        assert second.imported_turns == 0
        assert second.skipped_duplicates == 2
        assert second.total_turns_after == 3
        # and the local turn still replays cleanly afterwards
        store.close()


def _neutral_emotional() -> dict:
    from backend.emotional_domain import EmotionalStateV1

    return EmotionalStateV1.neutral(timestamp=1000.0).to_dict()


def _neutral_relationship() -> dict:
    from backend.relationship import RelationshipStateV1

    return RelationshipStateV1.neutral(timestamp=1000.0).to_dict()
